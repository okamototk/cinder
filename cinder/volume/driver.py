# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Drivers for volumes.

"""

import os
import socket
import time

from oslo.config import cfg

from cinder.brick.initiator import connector as initiator
from cinder import exception
from cinder.image import image_utils
from cinder.openstack.common import log as logging
from cinder import utils

LOG = logging.getLogger(__name__)

volume_opts = [
    cfg.IntOpt('num_shell_tries',
               default=3,
               help='number of times to attempt to run flakey shell commands'),
    cfg.IntOpt('reserved_percentage',
               default=0,
               help='The percentage of backend capacity is reserved'),
    cfg.IntOpt('num_iscsi_scan_tries',
               default=3,
               help='number of times to rescan iSCSI target to find volume'),
    cfg.IntOpt('iscsi_num_targets',
               default=100,
               help='Number of iscsi target ids per host'),
    cfg.StrOpt('iscsi_target_prefix',
               default='iqn.2010-10.org.openstack:',
               help='prefix for iscsi volumes'),
    cfg.StrOpt('iscsi_ip_address',
               default='$my_ip',
               help='The port that the iSCSI daemon is listening on'),
    cfg.IntOpt('iscsi_port',
               default=3260,
               help='The port that the iSCSI daemon is listening on'),
    cfg.StrOpt('volume_backend_name',
               default=None,
               help='The backend name for a given driver implementation'),
    cfg.BoolOpt('use_multipath_for_image_xfer',
                default=False,
                help='Do we attach/detach volumes in cinder using multipath '
                     'for volume to image and image to volume transfers?'), ]

CONF = cfg.CONF
CONF.register_opts(volume_opts)
CONF.import_opt('iscsi_helper', 'cinder.brick.iscsi.iscsi')


class VolumeDriver(object):
    """Executes commands relating to Volumes."""
    def __init__(self, execute=utils.execute, *args, **kwargs):
        # NOTE(vish): db is set by Manager
        self.db = None
        self.configuration = kwargs.get('configuration', None)
        if self.configuration:
            self.configuration.append_config_values(volume_opts)
        self.set_execute(execute)
        self._stats = {}

    def set_execute(self, execute):
        self._execute = execute

    def _is_non_recoverable(self, err, non_recoverable_list):
        for item in non_recoverable_list:
            if item in err:
                return True

        return False

    def _try_execute(self, *command, **kwargs):
        # NOTE(vish): Volume commands can partially fail due to timing, but
        #             running them a second time on failure will usually
        #             recover nicely.

        non_recoverable = kwargs.pop('no_retry_list', [])

        tries = 0
        while True:
            try:
                self._execute(*command, **kwargs)
                return True
            except exception.ProcessExecutionError as ex:
                tries = tries + 1

                if tries >= self.configuration.num_shell_tries or\
                        self._is_non_recoverable(ex.stderr, non_recoverable):
                    raise

                LOG.exception(_("Recovering from a failed execute.  "
                                "Try number %s"), tries)
                time.sleep(tries ** 2)

    def check_for_setup_error(self):
        raise NotImplementedError()

    def create_volume(self, volume):
        """Creates a volume. Can optionally return a Dictionary of
        changes to the volume object to be persisted.
        """
        raise NotImplementedError()

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        raise NotImplementedError()

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""
        raise NotImplementedError()

    def delete_volume(self, volume):
        """Deletes a volume."""
        raise NotImplementedError()

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
        raise NotImplementedError()

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
        raise NotImplementedError()

    def local_path(self, volume):
        raise NotImplementedError()

    def ensure_export(self, context, volume):
        """Synchronously recreates an export for a volume."""
        raise NotImplementedError()

    def create_export(self, context, volume):
        """Exports the volume. Can optionally return a Dictionary of changes
        to the volume object to be persisted.
        """
        raise NotImplementedError()

    def remove_export(self, context, volume):
        """Removes an export for a volume."""
        raise NotImplementedError()

    def initialize_connection(self, volume, connector):
        """Allow connection to connector and return connection info."""
        raise NotImplementedError()

    def terminate_connection(self, volume, connector, **kwargs):
        """Disallow connection from connector"""
        raise NotImplementedError()

    def attach_volume(self, context, volume, instance_uuid, host_name,
                      mountpoint):
        """Callback for volume attached to instance or host."""
        pass

    def detach_volume(self, context, volume):
        """Callback for volume detached."""
        pass

    def get_volume_stats(self, refresh=False):
        """Return the current state of the volume service. If 'refresh' is
           True, run the update first.
        """
        return None

    def do_setup(self, context):
        """Any initialization the volume driver does while starting"""
        pass

    def validate_connector(self, connector):
        """Fail if connector doesn't contain all the data needed by driver"""
        pass

    def copy_image_to_volume(self, context, volume, image_service, image_id):
        """Fetch the image from image_service and write it to the volume."""
        LOG.debug(_('copy_image_to_volume %s.') % volume['name'])

        properties = initiator.get_connector_properties()
        connection, device, connector = self._attach_volume(context, volume,
                                                            properties)

        try:
            image_utils.fetch_to_raw(context,
                                     image_service,
                                     image_id,
                                     device['path'])
        finally:
            self._detach_volume(connection, device, connector)
            self.terminate_connection(volume, properties)

    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        """Copy the volume to the specified image."""
        LOG.debug(_('copy_volume_to_image %s.') % volume['name'])

        properties = initiator.get_connector_properties()
        connection, device, connector = self._attach_volume(context, volume,
                                                            properties)

        try:
            image_utils.upload_volume(context,
                                      image_service,
                                      image_meta,
                                      device['path'])
        finally:
            self._detach_volume(connection, device, connector)
            self.terminate_connection(volume, properties)

    def _attach_volume(self, context, volume, properties):
        """Attach the volume."""
        host_device = None
        conn = self.initialize_connection(volume, properties)

        # Use Brick's code to do attach/detach
        use_multipath = self.configuration.use_multipath_for_image_xfer
        protocol = conn['driver_volume_type']
        connector = initiator.InitiatorConnector.factory(protocol,
                                                         use_multipath=
                                                         use_multipath)
        device = connector.connect_volume(conn['data'])
        host_device = device['path']

        if not connector.check_valid_device(host_device):
            raise exception.DeviceUnavailable(path=host_device,
                                              reason=(_("Unable to access "
                                                        "the backend storage "
                                                        "via the path "
                                                        "%(path)s.") %
                                                      {'path': host_device}))
        return conn, device, connector

    def _detach_volume(self, connection, device, connector):
        """Disconnect the volume from the host."""
        protocol = connection['driver_volume_type']
        # Use Brick's code to do attach/detach
        connector.disconnect_volume(connection['data'], device)

    def clone_image(self, volume, image_location):
        """Create a volume efficiently from an existing image.

        image_location is a string whose format depends on the
        image service backend in use. The driver should use it
        to determine whether cloning is possible.

        Returns a dict of volume properties eg. provider_location,
        boolean indicating whether cloning occurred
        """
        return None, False

    def backup_volume(self, context, backup, backup_service):
        """Create a new backup from an existing volume."""
        raise NotImplementedError()

    def restore_backup(self, context, backup, volume, backup_service):
        """Restore an existing backup to a new or existing volume."""
        raise NotImplementedError()

    def clear_download(self, context, volume):
        """Clean up after an interrupted image copy."""
        pass

    def extend_volume(self, volume, new_size):
        msg = _("Extend volume not implemented")
        raise NotImplementedError(msg)


class ISCSIDriver(VolumeDriver):
    """Executes commands relating to ISCSI volumes.

    We make use of model provider properties as follows:

    ``provider_location``
      if present, contains the iSCSI target information in the same
      format as an ietadm discovery
      i.e. '<ip>:<port>,<portal> <target IQN>'

    ``provider_auth``
      if present, contains a space-separated triple:
      '<auth method> <auth username> <auth password>'.
      `CHAP` is the only auth_method in use at the moment.
    """

    def __init__(self, *args, **kwargs):
        super(ISCSIDriver, self).__init__(*args, **kwargs)

    def _do_iscsi_discovery(self, volume):
        #TODO(justinsb): Deprecate discovery and use stored info
        #NOTE(justinsb): Discovery won't work with CHAP-secured targets (?)
        LOG.warn(_("ISCSI provider_location not stored, using discovery"))

        volume_name = volume['name']

        (out, _err) = self._execute('iscsiadm', '-m', 'discovery',
                                    '-t', 'sendtargets', '-p', volume['host'],
                                    run_as_root=True)
        for target in out.splitlines():
            if (self.configuration.iscsi_ip_address in target
                    and volume_name in target):
                return target
        return None

    def _get_iscsi_properties(self, volume):
        """Gets iscsi configuration

        We ideally get saved information in the volume entity, but fall back
        to discovery if need be. Discovery may be completely removed in future
        The properties are:

        :target_discovered:    boolean indicating whether discovery was used

        :target_iqn:    the IQN of the iSCSI target

        :target_portal:    the portal of the iSCSI target

        :target_lun:    the lun of the iSCSI target

        :volume_id:    the id of the volume (currently used by xen)

        :auth_method:, :auth_username:, :auth_password:

            the authentication details. Right now, either auth_method is not
            present meaning no authentication, or auth_method == `CHAP`
            meaning use CHAP with the specified credentials.
        """

        properties = {}

        location = volume['provider_location']

        if location:
            # provider_location is the same format as iSCSI discovery output
            properties['target_discovered'] = False
        else:
            location = self._do_iscsi_discovery(volume)

            if not location:
                msg = (_("Could not find iSCSI export for volume %s") %
                        (volume['name']))
                raise exception.InvalidVolume(reason=msg)

            LOG.debug(_("ISCSI Discovery: Found %s") % (location))
            properties['target_discovered'] = True

        results = location.split(" ")
        properties['target_portal'] = results[0].split(",")[0]
        properties['target_iqn'] = results[1]
        try:
            properties['target_lun'] = int(results[2])
        except (IndexError, ValueError):
            if (self.configuration.volume_driver in
                    ['cinder.volume.drivers.lvm.LVMISCSIDriver',
                     'cinder.volume.drivers.lvm.ThinLVMVolumeDriver'] and
                    self.configuration.iscsi_helper == 'tgtadm'):
                properties['target_lun'] = 1
            else:
                properties['target_lun'] = 0

        properties['volume_id'] = volume['id']

        auth = volume['provider_auth']
        if auth:
            (auth_method, auth_username, auth_secret) = auth.split()

            properties['auth_method'] = auth_method
            properties['auth_username'] = auth_username
            properties['auth_password'] = auth_secret

        geometry = volume.get('provider_geometry', None)
        if geometry:
            (physical_block_size, logical_block_size) = geometry.split()
            properties['physical_block_size'] = physical_block_size
            properties['logical_block_size'] = logical_block_size

        return properties

    def _run_iscsiadm(self, iscsi_properties, iscsi_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        (out, err) = self._execute('iscsiadm', '-m', 'node', '-T',
                                   iscsi_properties['target_iqn'],
                                   '-p', iscsi_properties['target_portal'],
                                   *iscsi_command, run_as_root=True,
                                   check_exit_code=check_exit_code)
        LOG.debug("iscsiadm %s: stdout=%s stderr=%s" %
                  (iscsi_command, out, err))
        return (out, err)

    def _run_iscsiadm_bare(self, iscsi_command, **kwargs):
        check_exit_code = kwargs.pop('check_exit_code', 0)
        (out, err) = self._execute('iscsiadm',
                                   *iscsi_command,
                                   run_as_root=True,
                                   check_exit_code=check_exit_code)
        LOG.debug("iscsiadm %s: stdout=%s stderr=%s" %
                  (iscsi_command, out, err))
        return (out, err)

    def _iscsiadm_update(self, iscsi_properties, property_key, property_value,
                         **kwargs):
        iscsi_command = ('--op', 'update', '-n', property_key,
                         '-v', property_value)
        return self._run_iscsiadm(iscsi_properties, iscsi_command, **kwargs)

    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info.

        The iscsi driver returns a driver_volume_type of 'iscsi'.
        The format of the driver data is defined in _get_iscsi_properties.
        Example return value::

            {
                'driver_volume_type': 'iscsi'
                'data': {
                    'target_discovered': True,
                    'target_iqn': 'iqn.2010-10.org.openstack:volume-00000001',
                    'target_portal': '127.0.0.0.1:3260',
                    'volume_id': 1,
                }
            }

        """

        if CONF.iscsi_helper == 'lioadm':
            self.tgtadm.initialize_connection(volume, connector)

        iscsi_properties = self._get_iscsi_properties(volume)
        return {
            'driver_volume_type': 'iscsi',
            'data': iscsi_properties
        }

    def validate_connector(self, connector):
        # iSCSI drivers require the initiator information
        if 'initiator' not in connector:
            err_msg = (_('The volume driver requires the iSCSI initiator '
                         'name in the connector.'))
            LOG.error(err_msg)
            raise exception.VolumeBackendAPIException(data=err_msg)

    def terminate_connection(self, volume, connector, **kwargs):
        pass

    def _get_iscsi_initiator(self):
        """Get iscsi initiator name for this machine"""
        # NOTE openiscsi stores initiator name in a file that
        #      needs root permission to read.
        contents = utils.read_file_as_root('/etc/iscsi/initiatorname.iscsi')
        for l in contents.split('\n'):
            if l.startswith('InitiatorName='):
                return l[l.index('=') + 1:].strip()

    def get_volume_stats(self, refresh=False):
        """Get volume status.

        If 'refresh' is True, run update the stats first.
        """
        if refresh:
            self._update_volume_status()

        return self._stats

    def _update_volume_status(self):
        """Retrieve status info from volume group."""

        LOG.debug(_("Updating volume status"))
        data = {}
        backend_name = self.configuration.safe_get('volume_backend_name')
        data["volume_backend_name"] = backend_name or 'Generic_iSCSI'
        data["vendor_name"] = 'Open Source'
        data["driver_version"] = '1.0'
        data["storage_protocol"] = 'iSCSI'

        data['total_capacity_gb'] = 'infinite'
        data['free_capacity_gb'] = 'infinite'
        data['reserved_percentage'] = 100
        data['QoS_support'] = False
        self._stats = data

    def accept_transfer(self, volume):
        pass


class FakeISCSIDriver(ISCSIDriver):
    """Logs calls instead of executing."""
    def __init__(self, *args, **kwargs):
        super(FakeISCSIDriver, self).__init__(execute=self.fake_execute,
                                              *args, **kwargs)

    def create_volume(self, volume):
        pass

    def check_for_setup_error(self):
        """No setup necessary in fake mode."""
        pass

    def initialize_connection(self, volume, connector):
        return {
            'driver_volume_type': 'iscsi',
            'data': {}
        }

    def terminate_connection(self, volume, connector, **kwargs):
        pass

    @staticmethod
    def fake_execute(cmd, *_args, **_kwargs):
        """Execute that simply logs the command."""
        LOG.debug(_("FAKE ISCSI: %s"), cmd)
        return (None, None)


class FibreChannelDriver(VolumeDriver):
    """Executes commands relating to Fibre Channel volumes."""
    def __init__(self, *args, **kwargs):
        super(FibreChannelDriver, self).__init__(*args, **kwargs)

    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info.

        The  driver returns a driver_volume_type of 'fibre_channel'.
        The target_wwn can be a single entry or a list of wwns that
        correspond to the list of remote wwn(s) that will export the volume.
        Example return values:

            {
                'driver_volume_type': 'fibre_channel'
                'data': {
                    'target_discovered': True,
                    'target_lun': 1,
                    'target_wwn': '1234567890123',
                }
            }

            or

             {
                'driver_volume_type': 'fibre_channel'
                'data': {
                    'target_discovered': True,
                    'target_lun': 1,
                    'target_wwn': ['1234567890123', '0987654321321'],
                }
            }

        """
        msg = _("Driver must implement initialize_connection")
        raise NotImplementedError(msg)
