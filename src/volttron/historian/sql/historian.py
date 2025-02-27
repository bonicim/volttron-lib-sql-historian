# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright 2022, Battelle Memorial Institute.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This material was prepared as an account of work sponsored by an agency of
# the United States Government. Neither the United States Government nor the
# United States Department of Energy, nor Battelle, nor any of their
# employees, nor any jurisdiction or organization that has cooperated in the
# development of these materials, makes any warranty, express or
# implied, or assumes any legal liability or responsibility for the accuracy,
# completeness, or usefulness or any information, apparatus, product,
# software, or process disclosed, or represents that its use would not infringe
# privately owned rights. Reference herein to any specific commercial product,
# process, or service by trade name, trademark, manufacturer, or otherwise
# does not necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# Battelle Memorial Institute. The views and opinions of authors expressed
# herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY operated by
# BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830
# }}}


import logging
import sys
import threading

from volttron import utils
from volttron.historian.base import BaseHistorian
from volttron.historian.sql import sqlutils
#from volttron.utils. import doc_inherit

__version__ = "4.0.0"

utils.setup_logging()
_log = logging.getLogger(__name__)


class MaskedString(str):
    def __repr__(self):
        return repr('********')


def historian(config_path, **kwargs):
    """
    This method is called by the :py:func:`sqlhistorian.historian.main` to
    parse the passed config file or configuration dictionary object, validate
    the configuration entries, and create an instance of SQLHistorian
    :param config_path: could be a path to a configuration file or can be a
                        dictionary object
    :param kwargs: additional keyword arguments if any
    :return: an instance of :py:class:`sqlhistorian.historian.SQLHistorian`
    """
    if isinstance(config_path, dict):
        config_dict = config_path
    else:
        config_dict = utils.load_config(config_path)

    connection = config_dict.get('connection', None)

    assert connection is not None
    database_type = connection.get('type', None)
    assert database_type is not None
    params = connection.get('params', None)
    assert params is not None

    # Avoid printing passwords in the debug message
    for key in ['pass', 'passwd', 'password', 'pw']:
        try:
            params[key] = MaskedString(params[key])
        except KeyError:
            pass

    SQLHistorian.__name__ = 'SQLHistorian'
    utils.update_kwargs_with_config(kwargs, config_dict)
    _log.debug("In sql historian before calling class kwargs is {}".format(kwargs))
    return SQLHistorian(**kwargs)


class SQLHistorian(BaseHistorian):
    """
    This is a historian agent that writes data to a SQLite or Mysql
    database based on the connection parameters in the configuration.
    .. seealso::
     - :py:mod:`volttron.platform.dbutils.basedb`
     - :py:mod:`volttron.platform.dbutils.mysqlfuncts`
     - :py:mod:`volttron.platform.dbutils.sqlitefuncts`
    """

    def __init__(self, connection, tables_def=None, **kwargs):
        """Initialise the historian.

        The historian makes two connections to the data store.  Both of
        these connections are available across the main and processing
        thread of the historian.  topic_map and topic_meta are used as
        cache for the meta data and topic maps.

        :param connection: dictionary that contains necessary information to
        establish a connection to the sql database. The dictionary should
        contain two entries -

          1. 'type' - describe the type of database (sqlite or mysql)
          2. 'params' - parameters for connecting to the database.

        :param tables_def: optional parameter. dictionary containing the
        names to be used for historian tables. Should contain the following
        keys

          1. "table_prefix": - if specified tables names are prefixed with
          this value followed by a underscore
          2."data_table": name of the table that stores historian data,
          3."topics_table": name of the table that stores the list of topics
          for which historian contains data data
          4. "meta_table": name of the table that stores the metadata data
          for topics

        :param kwargs: additional keyword arguments.
        """
        self.connection = connection
        self.tables_def, self.table_names = self.parse_table_def(tables_def)
        self.topic_id_map = {}
        self.topic_name_map = {}
        self.topic_meta = {}
        self.agg_topic_id_map = {}
        # Create two instance so connection is shared within a single thread.
        # This is because sqlite only supports sharing of connection within
        # a single thread.
        # historian_setup and publish_to_historian happens in background thread
        # everything else happens in the MainThread

        # One utils class instance( hence one db connection) for main thread
        self.main_thread_dbutils = self.get_dbfuncts_object()
        # One utils class instance( hence one db connection) for background thread
        # this gets initialized in the bg_thread within historian_setup
        self.bg_thread_dbutils = None
        super(SQLHistorian, self).__init__(**kwargs)

    def manage_db_size(self, history_limit_timestamp, storage_limit_gb):
        """
        Optional function to manage database size.
        """
        self.bg_thread_dbutils.manage_db_size(history_limit_timestamp, storage_limit_gb)

    #@doc_inherit
    def version(self):
        return __version__

    #@doc_inherit
    def publish_to_historian(self, to_publish_list):
        try:
            published = 0
            with self.bg_thread_dbutils.bulk_insert() as insert_data, \
                self.bg_thread_dbutils.bulk_insert_meta() as insert_meta:

                for x in to_publish_list:
                    ts = x['timestamp']
                    topic = x['topic']
                    value = x['value']
                    meta = x['meta']

                    # look at the topics that are stored in the database already to see if this topic has a value
                    lowercase_name = topic.lower()
                    topic_id = self.topic_id_map.get(lowercase_name, None)
                    db_topic_name = self.topic_name_map.get(lowercase_name,
                                                            None)
                    old_meta = self.topic_meta.get(topic_id, {})
                    update_topic_meta = True
                    if topic_id is None:
                        # send metadata data too. If topics table contains metadata column too it will get inserted
                        topic_id = self.bg_thread_dbutils.insert_topic(topic, metadata=meta)
                        # user lower case topic name when storing in map for case insensitive comparison
                        self.topic_name_map[lowercase_name] = topic
                        self.topic_id_map[lowercase_name] = topic_id
                        update_topic_meta = False
                    elif db_topic_name != topic:
                        if old_meta != meta:
                            _log.debug(f"META HAS CHANGED TOO. old:{old_meta} new:{meta}")
                            # pass metadata if metadata is stored in topics table metadata will get updated too
                            # if not will get ignored
                            self.bg_thread_dbutils.update_topic(topic, topic_id, metadata=meta)
                            update_topic_meta = False
                        else:
                            self.bg_thread_dbutils.update_topic(topic, topic_id)
                        self.topic_name_map[lowercase_name] = topic

                    if old_meta != meta:
                        if self.bg_thread_dbutils.topics_table != self.bg_thread_dbutils.meta_table:
                            # there is a separate metadata table. do bulk insert
                            _log.debug("meta in separate table")
                            insert_meta(topic_id, meta)
                        elif update_topic_meta:
                            _log.debug(" meta in same table. no topic change only meta changed")
                            # topic name and metadata are in same table, and metadata has not got into db during insert
                            # or update of topic so update meta alone in topics table
                            self.bg_thread_dbutils.update_meta(metadata=meta, topic_id=topic_id)

                        # either way update cache
                        self.topic_meta[topic_id] = meta

                    if insert_data(ts, topic_id, value):
                        published += 1

            if published:
                if self.bg_thread_dbutils.commit():
                    _log.debug("Reporting all handled")
                    self.report_all_handled()
                else:
                    _log.warning('Commit error. Rolling back {} values.'.format(published))
                    self.bg_thread_dbutils.rollback()
            else:
                _log.warning('Unable to publish {}'.format(len(to_publish_list)))
        except Exception as e:
            # TODO Unable to send alert from here
            # if isinstance(e, ConnectionError):
            #     _log.debug("Sending alert. Exception {}".format(e.args))
            #     err_message = "Unable to connect to database. Exception:{}".format(e.args)
            #     alert_id = DB_CONNECTION_FAILURE
            # else:
            #     err_message = "Unknown exception when publishing data. Exception: {}".format(e.args)
            #     alert_id = ERROR_PUBLISHING_DATA
            # self.vip.health.set_status(STATUS_BAD, err_message)
            # status = Status.from_json(self.vip.health.get_status())
            # self.vip.health.send_alert(alert_id, status)
            self.bg_thread_dbutils.rollback()
            # Raise to the platform so it is logged properly.
            raise

    #@doc_inherit
    def query_topic_list(self):

        _log.debug("query_topic_list Thread is: {}".format(threading.currentThread().getName()))
        if len(self.topic_name_map) > 0:
            return list(self.topic_name_map.values())
        else:
            # No topics present.
            return []

    #@doc_inherit
    def query_topics_by_pattern(self, topic_pattern):
        return self.main_thread_dbutils.query_topics_by_pattern(topic_pattern)

    #@doc_inherit
    def query_topics_metadata(self, topics):
        meta = {}
        if isinstance(topics, str):
            topic_id = self.topic_id_map.get(topics.lower())
            if topic_id:
                meta = {topics: self.topic_meta.get(topic_id)}
        elif isinstance(topics, list):
            for topic in topics:
                topic_id = self.topic_id_map.get(topic.lower())
                if topic_id:
                    meta[topic] = self.topic_meta.get(topic_id)
        return meta

    def query_aggregate_topics(self):
        return self.main_thread_dbutils.get_agg_topics()

    #@doc_inherit
    def query_historian(self, topic, start=None, end=None, agg_type=None, agg_period=None, skip=0, count=None,
                        order="FIRST_TO_LAST"):
        _log.debug("query_historian Thread is: {}".format(threading.currentThread().getName()))
        results = dict()
        topics_list = []
        if isinstance(topic, str):
            topics_list.append(topic)
        elif isinstance(topic, list):
            topics_list = topic

        multi_topic_query = len(topics_list) > 1

        topic_ids = []
        id_name_map = {}
        for topic in topics_list:
            topic_lower = topic.lower()
            topic_id = self.topic_id_map.get(topic_lower)
            if agg_type:
                agg_type = agg_type.lower()
                topic_id = self.agg_topic_id_map.get((topic_lower, agg_type, agg_period))
                if topic_id is None:
                    # load agg topic id again as it might be a newly configured aggregation
                    agg_map = self.main_thread_dbutils.get_agg_topic_map()
                    self.agg_topic_id_map.update(agg_map)
                    _log.debug(" Agg topic map after updating {} ".format(self.agg_topic_id_map))
                    topic_id = self.agg_topic_id_map.get((topic_lower, agg_type, agg_period))
            if topic_id:
                topic_ids.append(topic_id)
                id_name_map[topic_id] = topic
            else:
                _log.warning('No such topic {}'.format(topic))

        if not topic_ids:
            _log.warning('No topic ids found for topics{}. Returning empty result'.format(topics_list))
            return results

        _log.debug("Querying db reader with topic_ids {} ".format(topic_ids))

        values = self.main_thread_dbutils.query(topic_ids, id_name_map, start=start, end=end, agg_type=agg_type,
                                                agg_period=agg_period, skip=skip, count=count, order=order)
        meta_tid = None
        if len(values) > 0:
            # If there are results add metadata if it is a query on a single topic
            if not multi_topic_query:
                values = list(values.values())[0]
                if agg_type:
                    # if aggregation is on single topic find the topic id in the topics table that corresponds to
                    # agg_topic_id so that we can grab the correct metadata if topic name does not have entry in
                    # topic_id_map it is a user configured aggregation_topic_name which denotes aggregation across
                    # multiple points
                    _log.debug("Single topic aggregate query. Try to get metadata")
                    meta_tid = self.topic_id_map.get(topic.lower(), None)
                else:
                    # this is a query on raw data, get metadata for topic from topic_meta map
                    meta_tid = topic_ids[0]

            if values:
                metadata = self.topic_meta.get(meta_tid, {})
                results = {'values': values, 'metadata': metadata}
            else:
                results = dict()
        return results

    #@doc_inherit
    def historian_setup(self):
        thread_name = threading.currentThread().getName()
        _log.info("historian_setup on Thread: {}".format(thread_name))
        self.bg_thread_dbutils = self.get_dbfuncts_object()

        if not self._readonly:
            self.bg_thread_dbutils.setup_historian_tables()

        topic_id_map, topic_name_map = self.bg_thread_dbutils.get_topic_map()
        self.topic_id_map.update(topic_id_map)
        self.topic_name_map.update(topic_name_map)
        self.agg_topic_id_map = self.bg_thread_dbutils.get_agg_topic_map()
        topic_meta_map = self.bg_thread_dbutils.get_topic_meta_map()
        self.topic_meta.update(topic_meta_map)
        _log.debug(f"###DEBUG Loaded topics and metadata on start. Len of  topics {len(self.topic_id_map)} "
                   f"Len of metadata: {len(self.topic_meta)}")

    def get_dbfuncts_object(self):
        db_functs_class = sqlutils.get_dbfuncts_class(self.connection['type'])
        return db_functs_class(self.connection['params'], self.table_names)


def main(argv=sys.argv):
    """
    Main entry point for the agent.
    """

    try:
        utils.vip_main(historian, version=__version__)
    except Exception as e:
        print(e)
        _log.exception('unhandled exception')


if __name__ == '__main__':
    # Entry point for script
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
