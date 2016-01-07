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
# Copyright (c) 2016 Reliance JIO Corporation
# Copyright (c) 2016 Shishir Gowda <shishir.gowda@ril.com>
# Most of this work is directly derived from ceph, swift and chunked drivers
"""Ceph Backup Service Implementation.

This driver supports backuping up ceph volumes to a s3 like object store.

It is capable of performing incremental backups.

If incremental backups are used, multiple backups of the same volume are stored
as snapshots so that minimal space is consumed in the object store and
restoring the volume takes a far reduced amount of time compared to a full
copy.

Note that Cinder supports restoring to a new volume or the original volume the
backup was taken from. For the latter case, a full copy is enforced since this
was deemed the safest action to take. It is therefore recommended to always
restore to a new volume (default).
"""

import fcntl
import os
import re
import subprocess
import time
#import boto
#import boto.s3.connection
import pdb
import eventlet
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import encodeutils
from oslo_utils import excutils
from oslo_utils import units

from cinder.backup import driver
from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder import utils
import cinder.volume.drivers.rbd as rbd_driver

try:
    import rbd
except ImportError:
    rbd = None

LOG = logging.getLogger(__name__)

service_opts = [
    cfg.StrOpt('sbs_access_key', default='',
               help='Access key for S3 store.'),
    cfg.StrOpt('sbs_secret_key', default='',
               help='Secrete key for S3 store.'),
    cfg.StrOpt('sbs_container', default='backups',
               help='Container in S3 store to save backups.'),

]

CONF = cfg.CONF
CONF.register_opts(service_opts)

class SBSBackupDriver(driver.BackupDriver):
    """Backup Cinder volumes to S3 like Object Store.

    The backup will be performed using incremental differential backups which
	 *should* give a performance gain.
    """
    def __init__(self, context, db_driver=None, execute=None):
        super(SBSBackupDriver, self).__init__(context, db_driver)
        self.rbd = rbd
        self._execute = execute or utils.execute
        self._access_key = encodeutils.safe_encode(CONF.sbs_access_key)
        self._secret_key = encodeutils.safe_encode(CONF.sbs_secret_key)
        self._container = encodeutils.safe_encode(CONF.sbs_container)

    def _get_backup_base_name(self, volume_id, backup_id=None,
                              diff_format=False):
        # Ensure no unicode
        rbd_image_name = encodeutils.safe_encode("volume-%s.backup.base" % volume_id)
        LOG.debug("rbd base image name: %s", rbd_image_name)
        return rbd_image_name

    def _get_rbd_image_name(backup):
        rbd_image_name =  encodeutils.safe_encode("backup.%s.snap.%s" %
                                                 (backup['id'], backup['created_at']))
        LOG.debug("rbd image name: %s", rbd_image_name)
        return rbd_image_name

    @staticmethod
    def backup_snapshot_name_pattern():
        """Returns the pattern used to match backup snapshots.

        It is essential that snapshots created for purposes other than backups
        do not have this name format.
        """
        return r"^backup\.([a-z0-9\-]+?)\.snap\.(.+)$"


    def _get_new_snap_name(self, backup_id):
        return encodeutils.safe_encode("backup.%s.snap.%s" %
                                       (backup_id, time.time()))

    def _get_volume_size_gb(self, volume):
        """Return the size in gigabytes of the given volume.

        Raises exception.InvalidParameterValue if volume size is 0.
        """
        if int(volume['size']) == 0:
            errmsg = _("Need non-zero volume size")
            raise exception.InvalidParameterValue(errmsg)

        return int(volume['size']) * units.Gi

    def _validate_string_args(self, *args):
        """Ensure all args are non-None and non-empty."""
        return all(args)

    def _ceph_args(self, user, conf=None, pool=None):
        """Create default ceph args for executing rbd commands.

        If no --conf is provided, rbd will look in the default locations e.g.
        /etc/ceph/ceph.conf
        """

        # Make sure user arg is valid since rbd command may not fail if
        # invalid/no user provided, resulting in unexpected behaviour.
        if not self._validate_string_args(user):
            raise exception.BackupInvalidCephArgs(_("invalid user '%s'") %
                                                  user)

        args = ['--id', user]
        if conf:
            args.extend(['--conf', conf])
        if pool:
            args.extend(['--pool', pool])

        return args

    @classmethod
    def get_backup_snaps(cls, rbd_image, sort=False):
        """Get all backup snapshots for the given rbd image.

        NOTE: this call is made public since these snapshots must be deleted
              before the base volume can be deleted.
        """
        snaps = rbd_image.list_snaps()

        backup_snaps = []
        for snap in snaps:
            search_key = cls.backup_snapshot_name_pattern()
            result = re.search(search_key, snap['name'])
            if result:
                backup_snaps.append({'name': result.group(0),
                                     'backup_id': result.group(1),
                                     'timestamp': result.group(2)})

        if sort:
            # Sort into ascending order of timestamp
            backup_snaps.sort(key=lambda x: x['timestamp'], reverse=True)

        return backup_snaps

    def _get_most_recent_snap(self, rbd_image):
        """Get the most recent backup snapshot of the provided image.

        Returns name of most recent backup snapshot or None if there are no
        backup snapshots.
        """
        backup_snaps = self.get_backup_snaps(rbd_image, sort=True)
        if not backup_snaps:
            return None

        return backup_snaps[0]['name']


    def _lookup_base(self, rbd_image):
        backup_snaps = self.get_backup_snaps(rbd_image, sort=False)
        if not backup_snaps:
            return None
        backup_snaps.sort(key=lambda x: x['timestamp'], reverse=False)
        return backup_snaps[0]['name']

	# shishir change this to work out of s3 or db 
    def _snap_exists(self, base_name, snap_name):
        #Return True if snapshot exists in base image
        return True

    def _upload_to_DSS(self, snap_name, volume_name, ceph_args, from_snap=None):
        cmd = ['rbd', 'export-diff'] + ceph_args
        #if from_snap is None, do full upload
        if from_snap is not None:
            cmd.extend(['--from-snap', from_snap])
        path = encodeutils.safe_encode("%s@%s" %
                                      (volume_name, snap_name))
        loc = encodeutils.safe_encode("/tmp/%s" % (snap_name))
        cmd.extend([path, loc])
        LOG.info(cmd)
        self._execute (*cmd, run_as_root=False)
        #shishir: boto to upload the file from loc
        return

    """
    1. If 1st snapshot or missing base or missing incr snap
        create new snapshot (without incr) and treat it as base
        take snapshot from base (incr) with given name (size might be 0)
        upload/store both base and incr snap
    2. If incr snapshot
        create incr snapshot w.r.t latest snap
        upload/store snapshot
    """

    def _check_create_base(self, volume_id, volume_file, volume_name, 
			   base_name, ceph_args, backup_host, backup_service, from_snap=None):

        #Create an incremental backup from an RBD image.
        rbd_user = volume_file.rbd_user
        rbd_pool = volume_file.rbd_pool
        rbd_conf = volume_file.rbd_conf
        source_rbd_image = volume_file.rbd_image
        # Check if base image exists in dest
        found_base_image = self._lookup_base(source_rbd_image)
        #If base image not found, create base image, might be 1st snap
        if not found_base_image:
            # since base image is missing, default to full snap.Cleanup too
            if from_snap:
                LOG.debug("Source snapshot '%(snapshot)s' of volume "
                          "%(volume)s is stale so deleting.",
                          {'snapshot': from_snap, 'volume': volume_id})
                source_rbd_image.remove_snap(from_snap)
                source_rbd_image.remove_snap(base_name)
                from_snap = None

            #shishir: update size
            #Create new base image and upload it, so from-snap also becomes base
            LOG.debug ("Creating base image %s for volume %s" % (base_name, volume_id))
            source_rbd_image.create_snap(base_name)
            desc = (_("Base image of volume '%(volume)s'") % {'volume':volume_id})
            options = {'user_id': self.context.user_id,
                       'project_id': self.context.project_id,
                       'display_name': base_name,
                       'display_description': desc,
                       'volume_id': volume_id,
                       'id': volume_id,
                       'status': 'available',
                       'container': self._container,
		       'host': backup_host,
		       'service': 'cinder.backup.drivers.sbs',
                       'size': "2",
                      }
            backup = self.db.backup_create(self.context, options)
            self._upload_to_DSS(base_name, volume_name, ceph_args)
            from_snap = base_name
        else:
            # If a from_snap is defined but does not exist in the back base
            # then we cannot proceed (see above)
            if not self._snap_exists(base_name, from_snap):
                errmsg = (_("Snapshot='%(snap)s' does not exist in base "
                            "image='%(base)s' - aborting incremental "
                            "backup") %
                          {'snap': from_snap, 'base': base_name})
                LOG.info(errmsg)
                # Raise this exception so that caller can try another
                # approach
                raise exception.BackupRBDOperationFailed(errmsg)


        return (base_name, from_snap)

    def _backup_rbd(self, backup, volume_file, volume):
        #Create an incremental backup from an RBD image.
        rbd_user = volume_file.rbd_user
        rbd_pool = volume_file.rbd_pool
        rbd_conf = volume_file.rbd_conf
        source_rbd_image = volume_file.rbd_image
        backup_id = backup['id']
        backup_host = backup['host']
        backup_service = backup['service']
        volume_id = volume['id']
        volume_name = volume['name']

        # Identify our --from-snap point (if one exists)
        from_snap = self._get_most_recent_snap(source_rbd_image)
        base_name = self._get_backup_base_name(volume_id, diff_format=True)
        ceph_args = self._ceph_args(rbd_user, rbd_conf, pool=rbd_pool)

        #check base snap and from_snap and create base if missing
        base_name, from_snap = self._check_create_base(volume_id, volume_file,
                                                       volume_name, base_name,
                                                       ceph_args, backup_host,
                                                       backup_service, from_snap)

        # Snapshot source volume so that we have a new point-in-time
        if backup['display_name']:
            new_snap = backup['display_name']
        else:
            new_snap = self._get_new_snap_name(backup_id)
        LOG.debug("Creating backup %s", new_snap)
        source_rbd_image.create_snap(new_snap)
        LOG.debug("Using --from-snap '%(snap)s' for incremental backup of "
                  "volume %(volume)s, with base image '%s(base)s'.",
                    {'snap': from_snap, 'volume': volume_id,
                     'base': base_name})

        # export diff now
        self._upload_to_DSS(new_snap, volume_name, ceph_args, from_snap)

        #shishir: change volume_id to latest snap_id
        if from_snap == base_name:
            par_id = volume_id
        else:
            search_key = SBSBackupDriver.backup_snapshot_name_pattern()
            result = re.search(search_key, from_snap)
            if result:
                par_id = result.group(1)

        self.db.backup_update(self.context, backup_id,
                              {'parent_id': par_id})
        #ideally, we should be building snap name from backup id
        # _get_rbd_image_name does this, but wrong timestamp
        self.db.backup_update(self.context, backup_id,
                              {'display_name': new_snap})
        self.db.backup_update(self.context, backup_id,
                              {'container': self._container})

        # Remove older from-snap from src, as new snap will be "New" from-snap
	# Do this is from-snap is not same as base snap, as it will be the first
	if from_snap != base_name:
        	source_rbd_image.remove_snap(from_snap)
        return

    #shishir: Generate/update _container/bucket name and use that in DSS
    def backup(self, backup, volume_file, backup_metadata=False):
        backup_id = backup['id']
        volume = self.db.volume_get(self.context,backup['volume_id'])
        volume_id = volume['id']

        LOG.debug("Starting backup of volume='%s'.", volume_id)

        # Ensure we are at the beginning of the volume
        volume_file.seek(0)
        length = self._get_volume_size_gb(volume)

        self._backup_rbd(backup, volume_file, volume)

        self.db.backup_update(self.context, backup_id,
                              {'container': self._container})
        return

    #return sorted list with base as [0], and backup as last[n-1]
    def _list_incr_backups(self, backup):
        parent_id = backup['parent_id']

        backup_tree = []
        backup_tree.append(backup)
        curr = backup
        while curr['parent_id']:
            parent_backup = self.db.backup_get(self.context,
                                               curr['parent_id'])
            LOG.debug("Got parent of backup %s as %s" % (curr['id'], curr['parent_id']))
            backup_tree.append(parent_backup)
            curr = parent_backup

        backup_tree.reverse()
        return backup_tree

    def _download_from_DSS(self, snap_name, volume_name, ceph_args):
        cmd = ['rbd', 'import-diff'] + ceph_args
        #if from_snap is None, do full upload
        path = encodeutils.safe_encode("%s" % (volume_name))
        loc = encodeutils.safe_encode("/tmp/%s" % (snap_name))
        cmd.extend([loc, path])
        LOG.info("Downloading backups %s" % (cmd))
        self._execute (*cmd, run_as_root=False)
        #shishir: boto to upload the file from loc
        return

    def _restore_rbd(self, backup, volume_id, volume_file, ceph_args):
        backup_id = backup['id']
        backup_volume_id = backup['volume_id']
        # issue here is, we cant resolve timestamp suffix of rbd image
        #backup_name = self._get_rbd_image_name(backup)
        backup_name = backup['display_name']
        volume = self.db.volume_get(self.context,volume_id)
        length = int(volume['size']) * units.Gi
        volume_name = (_("volume-%s" % (volume['id'])))
        LOG.debug("Restoring backup %s to volume %s" % (backup_name, volume_name))
        # If the volume we are restoring to is the volume the backup was
        # made from, force a full restore since a diff will not work in
        # this case.
        if volume_id == backup_id:
            LOG.debug("Destination volume is same as backup source volume "
                      "%s - forcing full copy.", volume['id'])
            return False

        self._download_from_DSS(backup_name, volume_name, ceph_args) 
        return

    """
        Get backup and all its parent leading upto base
        Replay in reverse order, from base to specified backup
        resize image to original size, as it might get shrunk
        due to replay of diffs
    """
    def restore(self, backup, volume_id, volume_file):
        backup_id = backup['id']
        rbd_user = volume_file.rbd_user
        rbd_pool = volume_file.rbd_pool
        rbd_conf = volume_file.rbd_conf
        backup_tree = self._list_incr_backups(backup)
        ceph_args = self._ceph_args(rbd_user, rbd_conf, pool=rbd_pool)
        backup_layers = len(backup_tree)
        try:
            i = 0
            while i < backup_layers:
                backup_diff = backup_tree[i]
                self._restore_rbd(backup_diff, volume_id, volume_file,
                                  ceph_args)
                i = i+1

            # Be tolerant of IO implementations that do not support fileno()
            try:
                fileno = volume_file.fileno()
            except IOError:
                LOG.debug("Restore target I/O object does not support "
                          "fileno() - skipping call to fsync().")
            else:
                os.fsync(fileno)

            LOG.debug('restore %(backup_id)s to %(volume_id)s finished.',
                      {'backup_id': backup_id, 'volume_id': volume_id})

        except exception.BackupOperationError as e:
            LOG.error(_LE('Restore to volume %(volume)s finished with error - '
                          '%(error)s.'), {'error': e, 'volume': volume_id})
            raise
        return

    def delete(self, backup):
        return

def get_backup_driver(context):
    return SBSBackupDriver(context)
