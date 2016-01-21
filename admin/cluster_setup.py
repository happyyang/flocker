# Copyright 2015 ClusterHQ Inc.  See LICENSE file for details.
"""
Set up a Flocker cluster.
"""

import stat
import string
import sys
import yaml
from pipes import quote as shell_quote

from eliot import add_destination, FileDestination


from twisted.internet.defer import inlineCallbacks
from twisted.python.usage import UsageError
from twisted.python.filepath import FilePath

from .acceptance import (
    ClusterIdentity,
    CommonOptions,
    capture_journal,
    capture_upstart,
    eliot_output,
    get_trial_environment,
)

from flocker.common import gather_deferreds


class RunOptions(CommonOptions):
    description = "Set up a Flocker cluster."

    optParameters = [
        ['purpose', None, 'testing',
         "Purpose of the cluster recorded in its metadata where possible."],
        ['cert-directory', None, None,
         "Directory for storing the cluster certificates. "
         "If not specified, then a temporary directory is used."],
    ]

    optFlags = [
        ["no-keep", None, "Do not keep VMs around (when testing)"],
    ]

    synopsis = ('Usage: cluster-setup --distribution <distribution> '
                '[--provider <provider>]')

    def __init__(self, top_level):
        """
        :param FilePath top_level: The top-level of the Flocker repository.
        """
        super(RunOptions, self).__init__(top_level)
        # Override default values defined in the base class.
        self['provider'] = self.defaults['provider'] = 'aws'
        self['dataset-backend'] = self.defaults['dataset-backend'] = 'aws'

    def postOptions(self):

        self['purpose'] = unicode(self['purpose'])
        if any(x not in string.ascii_letters + string.digits + '-'
               for x in self['purpose']):
            raise UsageError(
                "Purpose may have only alphanumeric symbols and dash. " +
                "Found {!r}".format('purpose')
            )

        if self['cert-directory']:
            cert_path = FilePath(self['cert-directory'])
            _ensure_empty_directory(cert_path)
            self['cert-directory'] = cert_path

        # This is run last as it creates the actual "runner" object
        # based on the provided parameters.
        super(RunOptions, self).postOptions()

    def _make_cluster_identity(self, dataset_backend):
        purpose = self['purpose']
        return ClusterIdentity(
            purpose=purpose,
            prefix=purpose,
            name='{}-cluster'.format(purpose).encode("ascii"),
        )


def _ensure_empty_directory(path):
    """
    The path should not exist or it should be an empty directory.
    If the path does not exist then a new directory is created.

    :param FilePath path: The directory path to check or create.
    """
    if path.exists():
        if not path.isdir():
            raise UsageError("{} is not a directory".format(path.path))
        if path.listdir():
            raise UsageError("{} is not empty".format(path.path))
        return

    try:
        path.makedirs()
        path.chmod(stat.S_IRWXU)
    except OSError as e:
        raise UsageError(
            "Can not create {}. {}: {}.".format(path.path, e.filename,
                                                e.strerror)
        )


def generate_managed_section(cluster):
    """
    Generate a managed configuration section for the given cluster.
    The section describes the nodes comprising the cluster.

    :param Cluster cluser: The cluster.
    :return: The managed configuration.
    :rtype: dict
    """
    addresses = list()
    for node in cluster.agent_nodes:
        if node.private_address is not None:
            addresses.append([node.private_address, node.address])
        else:
            addresses.append(node.address)
    return {
        "managed": {
            "addresses": addresses,
            "upgrade": True,
        }
    }


def create_managed_config(base_config, cluster):
    """
    Generate a full configuration from the given base configuration
    by adding a managed section for the given cluster instance.
    The base configuration should provide parameters like the dataset
    backend configurations and the cluster metadata.

    :param dict base_config: The base configuration.
    :param Cluster cluser: The cluster.
    :return: The new configuration with the managed section.
    :rtype: dict
    """
    config = dict(base_config)
    config.update(generate_managed_section(cluster))
    return config


@inlineCallbacks
def main(reactor, args, base_path, top_level):
    """
    :param reactor: Reactor to use.
    :param list args: The arguments passed to the script.
    :param FilePath base_path: The executable being run.
    :param FilePath top_level: The top-level of the Flocker repository.
    """
    options = RunOptions(top_level=top_level)

    add_destination(eliot_output)
    try:
        options.parseOptions(args)
    except UsageError as e:
        sys.stderr.write("%s: %s\n" % (base_path.basename(), e))
        raise SystemExit(1)

    runner = options.runner

    from flocker.common.script import eliot_logging_service
    log_writer = eliot_logging_service(
        destination=FileDestination(
            file=open("%s.log" % (base_path.basename(),), "a")
        ),
        reactor=reactor,
        capture_stdout=False)
    log_writer.startService()
    reactor.addSystemEventTrigger(
        'before', 'shutdown', log_writer.stopService)

    def cluster_cleanup():
        print("stopping cluster")
        return runner.stop_cluster(reactor)

    cleanup_trigger_id = reactor.addSystemEventTrigger('before', 'shutdown',
                                                       cluster_cleanup)

    results = []
    yield runner.ensure_keys(reactor)
    cluster = yield runner.start_cluster(reactor)

    managed_config_file = options['cert-directory'].child("managed.yaml")
    managed_config = create_managed_config(options['config'], cluster)
    managed_config_file.setContent(
        yaml.safe_dump(managed_config, default_flow_style=False)
    )

    if options['distribution'] in ('centos-7',):
        remote_logs_file = open("remote_logs.log", "a")
        for node in cluster.all_nodes:
            results.append(capture_journal(reactor,
                                           node.address,
                                           remote_logs_file)
                           )
    elif options['distribution'] in ('ubuntu-14.04', 'ubuntu-15.10'):
        remote_logs_file = open("remote_logs.log", "a")
        for node in cluster.all_nodes:
            results.append(capture_upstart(reactor,
                                           node.address,
                                           remote_logs_file)
                           )
    # gather_deferreds() below does more than just creating a DeferredList.
    # So, while we are not using its result in any way, it is still useful
    # because it logs any failures coming from the gathered deferreds.
    gather_deferreds(results)

    if options['no-keep']:
        print("not keeping cluster")
    else:
        environment_variables = get_trial_environment(cluster)
        environment_strings = list()
        for environment_variable in environment_variables:
            environment_strings.append(
                "export {name}={value};\n".format(
                    name=environment_variable,
                    value=shell_quote(
                        environment_variables[environment_variable]
                    ),
                )
            )
        environment = ''.join(environment_strings)
        print("The following variables describe the cluster:")
        print(environment)
        env_file = options['cert-directory'].child("environment.env")
        env_file.setContent(environment)
        print("The variables are also saved in {}".format(
            env_file.path
        ))
        print("Be sure to preserve the required files.")

        reactor.removeSystemEventTrigger(cleanup_trigger_id)
