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

from oslo_log import log as logging
from sqlalchemy import Column, MetaData, Table, Float, Integer

from cinder.i18n import _LE

LOG = logging.getLogger(__name__)


def upgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    backups = Table('backups', meta, autoload=True)
    actual_size = Column('actual_size', Integer(32))

    try:
        backups.create_column(actual_size)
        backups.update().values(actual_size=None).execute()
    except Exception:
        LOG.error(_LE("Adding actual_size column to backups table failed."))
        raise


def downgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine

    backups = Table('backups', meta, autoload=True)
    actual_size = backups.columns.actual_size

    try:
        backups.drop_column(actual_size)
    except Exception:
        LOG.error(_LE("Dropping actual_size column from backups table failed."))
        raise
