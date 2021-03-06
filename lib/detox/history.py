import os
import re
import sqlite3
import lzma
import hashlib
import signal
import logging

from dynamo.utils.interface.mysql import MySQL
from dynamo.dataformat import Site
from dynamo.operation.history import DeletionHistoryDatabase
from dynamo.dataformat import Configuration

LOG = logging.getLogger(__name__)

class DetoxHistoryBase(DeletionHistoryDatabase):
    """
    Parts of the DetoxHistory that can be used by the web detox monitor.
    """

    _config = Configuration()

    @staticmethod
    def set_default(config):
        DetoxHistoryBase._config = Configuration(config)

    def __init__(self, config = None):
        DeletionHistoryDatabase.__init__(self, config)

        # intentionally passing the config directly to DeletionHistoryDatabase
        if config is None:
            config = DetoxHistoryBase._config

        self.history_db = self.db.db_name()
        self.cache_db = config.cache_db
        self.snapshots_spool_dir = config.snapshots_spool_dir
        self.snapshots_archive_dir = config.snapshots_archive_dir

    def get_cycles(self, partition, first = -1, last = -1):
        """
        Get a list of deletion cycles in range first <= cycle <= last. If first == -1, pick only the latest before last.
        If last == -1, select cycles up to the latest.
        @param partition  partition name
        @param first      first cycle
        @param last       last cycle

        @return list of cycle numbers
        """

        result = self.db.query('SELECT `id` FROM `partitions` WHERE `name` LIKE %s', partition)
        if len(result) == 0:
            return []

        partition_id = result[0]

        sql = 'SELECT `id` FROM `deletion_cycles` WHERE `partition_id` = %s AND `time_end` NOT LIKE \'0000-00-00 00:00:00\' AND `operation` IN (\'deletion\', \'deletion_test\')'

        if first >= 0:
            sql += ' AND `id` >= %d' % first
        if last >= 0:
            sql += ' AND `id` <= %d' % last

        sql += ' ORDER BY `id` ASC'

        result = self.db.query(sql, partition_id)

        if first < 0 and len(result) > 1:
            result = result[-1:]

        return result

    def get_sites(self, cycle_number, skip_unused = False):
        """
        Collect the site status for a given cycle number or the latest cycle of the partition
        and return as a plain dict.
        @param cycle_number   Detox cycle number
        @param skip_unused    If true, don't list sites that had no data in the cycle

        @return {site_name:  (id, status, quota)}
        """

        self._fill_snapshot_cache('sites', cycle_number)

        table_name = 'sites_%d' % cycle_number

        sql = 'SELECT s.`name`, n.`status`, n.`quota` FROM `{0}`.`{1}` AS n'.format(self.cache_db, table_name)
        sql += ' INNER JOIN `{0}`.`sites` AS s ON s.`id` = n.`site_id`'.format(self.history_db)
        if skip_unused:
            self._fill_snapshot_cache('replicas', cycle_number)

            replica_table_name = 'replicas_%d' % cycle_number
            sql += ' INNER JOIN (SELECT DISTINCT `site_id` FROM `{0}`.`{1}`) AS r ON r.`site_id` = s.`id`'.format(self.cache_db, replica_table_name)

        sites_dict = {}

        for site_name, status, quota in self.db.xquery(sql):
            sites_dict[site_name] = (status, quota)

        return sites_dict

    def get_deletion_decisions(self, cycle_number, size_only = True, decisions = None):
        """
        @param cycle_number   Cycle number
        @param size_only      Boolean
        @param decisions      If a list, limit to specified decisions
        
        @return If size_only = True: a dict {site: (protect_size, delete_size, keep_size)}
                If size_only = False: a massive dict {site: [(dataset, size, decision, reason)]}
        """

        self._fill_snapshot_cache('replicas', cycle_number)

        table_name = 'replicas_%d' % cycle_number

        if size_only:
            # return {site_name: (protect_size, delete_size, keep_size)}
            volumes = {}
            sites = set()

            query = 'SELECT s.`name`, SUM(r.`size`) * 1.e-12 FROM `{0}`.`{1}` AS r'.format(self.cache_db, table_name)
            query += ' INNER JOIN `{0}`.`sites` AS s ON s.`id` = r.`site_id`'.format(self.history_db)
            query += ' WHERE r.`decision` LIKE %s'
            query += ' GROUP BY r.`site_id`'

            if type(decisions) is not list:
                decisions = ['protect', 'delete', 'keep']

            for decision in decisions:
                volumes[decision] = dict(self.db.xquery(query, decision))
                sites.update(set(volumes[decision].iterkeys()))
               
            product = {}
            for site_name in sites:
                v = {}
                for decision in ['protect', 'delete', 'keep']:
                    try:
                        v[decision] = volumes[decision][site_name]
                    except:
                        v[decision] = 0

                product[site_name] = (v['protect'], v['delete'], v['keep'])

            return product

        else:
            # return {site_name: [(dataset_name, size, decision, condition_id, reason)]}

            query = 'SELECT s.`name`, d.`name`, r.`size`, r.`decision`, r.`condition`, p.`text` FROM `{0}`.`{1}` AS r'.format(self.cache_db, table_name)
            query += ' INNER JOIN `{0}`.`sites` AS s ON s.`id` = r.`site_id`'.format(self.history_db)
            query += ' INNER JOIN `{0}`.`datasets` AS d ON d.`id` = r.`dataset_id`'.format(self.history_db)
            query += ' LEFT JOIN `{0}`.`policy_conditions` AS p ON p.`id` = r.`condition`'.format(self.history_db)
            if type(decisions) is list:
                query += ' WHERE r.`decision` IN (%s)' % ','.join('\'%s\'' % d for d in decisions)
            query += ' ORDER BY s.`name` ASC, r.`size` DESC'

            product = {}

            _site_name = ''

            for site_name, dataset_name, size, decision, cid, reason in self.db.xquery(query):
                if site_name != _site_name:
                    product[site_name] = []
                    current = product[site_name]
                    _site_name = site_name
                
                current.append((dataset_name, size, decision, cid, reason))

            return product

    def get_site_deletion_decisions(self, cycle_number, site_name):
        """
        @return  site-specific version of get_deletion_decisions with size_only = False
        """

        self._fill_snapshot_cache('replicas', cycle_number)

        table_name = 'replicas_%d' % cycle_number

        query = 'SELECT d.`name`, r.`size`, r.`decision`, r.`condition`, p.`text` FROM `{0}`.`{1}` AS r'.format(self.cache_db, table_name)
        query += ' INNER JOIN `{0}`.`sites` AS s ON s.`id` = r.`site_id`'.format(self.history_db)
        query += ' INNER JOIN `{0}`.`datasets` AS d ON d.`id` = r.`dataset_id`'.format(self.history_db)
        query += ' LEFT JOIN `{0}`.`policy_conditions` AS p ON p.`id` = r.`condition`'.format(self.history_db)
        query += ' WHERE s.`name` = %s ORDER BY r.`size` DESC'

        return self.db.query(query, site_name)

    def _fill_snapshot_cache(self, template, cycle_number):
        self.db.use_db(self.cache_db)

        # cycle_number is either a cycle number or a partition name. %s works for both
        table_name = '%s_%s' % (template, cycle_number)

        table_exists = self.db.table_exists(table_name)

        is_cycle = True
        try:
            cycle_number += 0
        except TypeError:
            is_cycle = False

        if not is_cycle or not table_exists:
            if is_cycle:
                db_file_name = '%s/snapshot_%09d.db' % (self.snapshots_spool_dir, cycle_number)

                if not os.path.exists(db_file_name):
                    try:
                        os.makedirs(self.snapshots_spool_dir)
                        os.chmod(self.snapshots_spool_dir, 0777)
                    except OSError:
                        pass

                    scycle = '%09d' % cycle_number
                    xz_file_name = '%s/%s/%s/snapshot_%09d.db.xz' % (self.snapshots_archive_dir, scycle[:3], scycle[3:6], cycle_number)
                    if not os.path.exists(xz_file_name):
                        raise RuntimeError('Archived snapshot DB ' + xz_file_name + ' does not exist')
    
                    with open(xz_file_name, 'rb') as xz_file:
                        with open(db_file_name, 'wb') as db_file:
                            db_file.write(lzma.decompress(xz_file.read()))

            else:
                db_file_name = '%s/snapshot_%s.db' % (self.snapshots_spool_dir, cycle_number)

                if not os.path.exists(db_file_name):
                    return

            # fill from sqlite
            if table_exists:
                self.db.query('TRUNCATE TABLE `{0}`'.format(table_name))
            else:
                self.db.query('CREATE TABLE `{0}` LIKE `{1}`'.format(table_name, template))

            snapshot_db = sqlite3.connect(db_file_name)
            snapshot_db.text_factory = str # otherwise we'll get unicode and MySQLdb cannot convert that
            snapshot_cursor = snapshot_db.cursor()

            def make_snapshot_reader():
                if template == 'replicas':
                    sql = 'SELECT r.`site_id`, r.`dataset_id`, r.`size`, d.`value`, r.`condition` FROM `replicas` AS r'
                    sql += ' INNER JOIN `decisions` AS d ON d.`id` = r.`decision_id`'
                elif template == 'sites':
                    sql = 'SELECT s.`site_id`, t.`value`, s.`quota` FROM `sites` AS s'
                    sql += ' INNER JOIN `statuses` AS t ON t.`id` = s.`status_id`'
                    
                snapshot_cursor.execute(sql)
                
                while True:
                    row = snapshot_cursor.fetchone()
                    if row is None:
                        return

                    yield row

            snapshot_reader = make_snapshot_reader()

            if template == 'replicas':
                fields = ('site_id', 'dataset_id', 'size', 'decision', 'condition')
            elif template == 'sites':
                fields = ('site_id', 'status', 'quota')
                
            self.db.insert_many(table_name, fields, None, snapshot_reader, do_update = False)

            snapshot_cursor.close()
            snapshot_db.close()

        if is_cycle:
            # cycle_number is really a number. Update the partition cache table too
            sql = 'SELECT p.`name` FROM `{hdb}`.`partitions` AS p INNER JOIN `{hdb}`.`deletion_cycles` AS r ON r.`partition_id` = p.`id` WHERE r.`id` = %s'.format(hdb = self.history_db)
            partition = self.db.query(sql, cycle_number)[0]
    
            self._fill_snapshot_cache(template, partition)

            # then update the cache usage
            self._update_cache_usage(template, cycle_number)

        self.db.use_db(self.history_db)

    def _update_cache_usage(self, template, cycle_number):
        self.db.use_db(self.cache_db)

        self.db.query('INSERT INTO `{template}_snapshot_usage` VALUES (%s, NOW())'.format(template = template), cycle_number)

        # clean old cache
        sql = 'SELECT `cycle_id` FROM (SELECT `cycle_id`, MAX(`timestamp`) AS m FROM `replicas_snapshot_usage` GROUP BY `cycle_id`) AS t WHERE m < DATE_SUB(NOW(), INTERVAL 1 WEEK)'
        old_replica_cycles = self.db.query(sql)
        for old_cycle in old_replica_cycles:
            table_name = 'replicas_%d' % old_cycle
            self.db.query('DROP TABLE IF EXISTS `{0}`'.format(table_name))

        sql = 'SELECT `cycle_id` FROM (SELECT `cycle_id`, MAX(`timestamp`) AS m FROM `sites_snapshot_usage` GROUP BY `cycle_id`) AS t WHERE m < DATE_SUB(NOW(), INTERVAL 1 WEEK)'
        old_site_cycles = self.db.query(sql)
        for old_cycle in old_site_cycles:
            table_name = 'sites_%d' % old_cycle
            self.db.query('DROP TABLE IF EXISTS `{0}`'.format(table_name))

        for old_cycle in set(old_replica_cycles) & set(old_site_cycles):
            scycle = '%09d' % old_cycle
            db_file_name = '%s/snapshot_%09d.db' % (self.snapshots_spool_dir, old_cycle)
            if os.path.exists(db_file_name):
                try:
                    os.unlink(db_file_name)
                except:
                    LOG.error('Failed to delete %s' % db_file_name)
                    pass

        self.db.query('DELETE FROM `replicas_snapshot_usage` WHERE `timestamp` < DATE_SUB(NOW(), INTERVAL 1 WEEK)')
        self.db.query('OPTIMIZE TABLE `replicas_snapshot_usage`')
        self.db.query('DELETE FROM `sites_snapshot_usage` WHERE `timestamp` < DATE_SUB(NOW(), INTERVAL 1 WEEK)')
        self.db.query('OPTIMIZE TABLE `sites_snapshot_usage`')


class DetoxHistory(DetoxHistoryBase):
    """
    Class for handling Detox history.
    """

    def new_cycle(self, partition, policy_text, comment = '', test = False):
        """
        Set up a new deletion cycle for the partition.
        @param partition        Partition name string
        @param policy_text      Full text of the policy
        @param comment          Comment string
        @param test             If True, create a deletion_test cycle.

        @return cycle number.
        """

        if self._read_only:
            return 0

        part_id = self.save_partitions([partition], get_ids = True)[0]

        policy_id = self.save_policy(policy_text)

        if test:
            operation_str = 'deletion_test'
        else:
            operation_str = 'deletion'

        columns = ('operation', 'partition_id', 'policy_id', 'comment', 'time_start')
        values = (operation_str, part_id, policy_id, comment, MySQL.bare('NOW()'))
        return self.db.insert_get_id('deletion_cycles', columns = columns, values = values)

    def close_cycle(self, cycle_number):
        """
        Finalize the records for the given cycle.
        @param cycle_number   Cycle number
        """

        if self._read_only:
            return

        self.db.query('UPDATE `deletion_cycles` SET `time_end` = NOW() WHERE `id` = %s', cycle_number)

    def save_policy(self, policy_text):
        md5 = hashlib.md5(policy_text).hexdigest()
        result = self.db.query('SELECT `id`, `text` FROM `deletion_policies` WHERE `hash` = UNHEX(%s)', md5)

        for policy_id, text in result:
            if text == policy_text:
                return policy_id

        # no row with matching hash or no row with matching text although hash matches (basically impossible)
        # new policy
        columns = ('hash', 'text')
        return self.db.insert_get_id('deletion_policies', columns = columns, values = (MySQL.bare('UNHEX(\'%s\')' % md5), policy_text))

    def save_conditions(self, policy_lines):
        """
        Save policy conditions and set condition_ids.
        @param policy_lines  List of PolicyLine objects
        """

        if self._read_only:
            return

        for line in policy_lines:
            text = re.sub('\s+', ' ', line.condition.text)
            sql = 'SELECT `id` FROM {0}.`policy_conditions` WHERE `text` = %s'.format(self.history_db)
            ids = self.db.query(sql, text)
            if len(ids) == 0:
                sql = 'INSERT INTO {0}.`policy_conditions` (`text`) VALUES (%s)'.format(self.history_db)
                self.db.query(sql, text)
                line.condition_id = self.db.last_insert_id
            else:
                line.condition_id = ids[0]

    def save_cycle_state(self, cycle_number, deleted_list, kept_list, protected_list, quotas):
        """
        Save decisions and their reasons for all replicas.
        @param cycle_number      Cycle number.
        @param deleted_list    {replica: [([block_replica], condition)]}
        @param kept_list       {replica: [([block_replica], condition)]}
        @param protected_list  {replica: [([block_replica], condition)]}
        @param quotas          {site: quota in TB}

        Note that in case of block-level operations, one dataset replica can appear
        in multiple of deleted, kept, and protected.
        """

        if self._read_only:
            return

        self.save_sites([s.name for s in quotas.iterkeys()])

        datasets = set()
        for replica, matches in deleted_list.iteritems():
            datasets.add(replica.dataset.name)
        for replica, matches in kept_list.iteritems():
            datasets.add(replica.dataset.name)
        for replica, matches in protected_list.iteritems():
            datasets.add(replica.dataset.name)

        self.save_datasets(datasets)
    
        reuse = self.db.reuse_connection
        self.db.reuse_connection = True

        self.db.use_db(self.cache_db)

        ## Replica state (deletion decisions)

        # Insert full data into a temporary table with site and dataset names
        tmp_table = 'replicas_tmp'
        columns = [
            '`site` varchar(32) CHARACTER SET latin1 COLLATE latin1_general_cs NOT NULL',
            '`dataset` varchar(512) CHARACTER SET latin1 COLLATE latin1_general_cs NOT NULL',
            '`size` bigint(20) unsigned NOT NULL',
            '`decision` enum(\'delete\',\'keep\',\'protect\') CHARACTER SET latin1 COLLATE latin1_general_ci NOT NULL',
            '`condition` int(10) unsigned NOT NULL',
            'KEY `site_dataset` (`site`,`dataset`)'
        ]
        self.db.create_tmp_table(tmp_table, columns)

        def replica_entry(entries, decision):
            for replica, matches in entries.iteritems():
                site_name = replica.site.name
                dataset_name = replica.dataset.name
                for condition_id, block_replicas in matches.iteritems():
                    size = sum(r.size for r in block_replicas)
                    yield (site_name, dataset_name, size, decision, condition_id)

        fields = ('site', 'dataset', 'size', 'decision', 'condition')
        self.db.insert_many(tmp_table, fields, None, replica_entry(deleted_list, 'delete'), do_update = False, db = self.db.scratch_db)
        self.db.insert_many(tmp_table, fields, None, replica_entry(kept_list, 'keep'), do_update = False, db = self.db.scratch_db)
        self.db.insert_many(tmp_table, fields, None, replica_entry(protected_list, 'protect'), do_update = False, db = self.db.scratch_db)

        # Make a snapshot table
        replica_table_name = 'replicas_%s' % cycle_number

        if self.db.table_exists(replica_table_name):
            self.db.query('DROP TABLE `{0}`'.format(replica_table_name))

        self.db.query('CREATE TABLE `{0}` LIKE `replicas`'.format(replica_table_name))

        # Then use insert select join to convert the names to ids
        sql = 'INSERT INTO `{0}` (`site_id`, `dataset_id`, `size`, `decision`, `condition`)'.format(replica_table_name)
        sql += ' SELECT s.`id`, d.`id`, r.`size`, r.`decision`, r.`condition` FROM `{0}`.`{1}` AS r'.format(self.db.scratch_db, tmp_table)
        sql += ' INNER JOIN `{0}`.`sites` AS s ON s.`name` = r.`site`'.format(self.history_db)
        sql += ' INNER JOIN `{0}`.`datasets` AS d ON d.`name` = r.`dataset`'.format(self.history_db)
        self.db.query(sql)

        self.db.drop_tmp_table(tmp_table)

        ## Site state (status and quotas)

        # Insert full data into a temporary table with site info
        tmp_table = 'sites_tmp'
        columns = [
            '`site` varchar(32) CHARACTER SET latin1 COLLATE latin1_general_cs NOT NULL',
            '`status` enum(\'ready\',\'waitroom\',\'morgue\',\'unknown\') CHARACTER SET latin1 COLLATE latin1_general_ci NOT NULL',
            '`quota` int(10) NOT NULL',
            'KEY `site` (`site`)'
        ]
        self.db.create_tmp_table(tmp_table, columns)

        fields = ('site', 'status', 'quota')
        mapping = lambda (site, quota): (site.name, site.status, quota)
        self.db.insert_many(tmp_table, fields, mapping, quotas.iteritems(), do_update = False, db = self.db.scratch_db)

        # Make a snapshot table
        site_table_name = 'sites_%s' % cycle_number

        if self.db.table_exists(site_table_name):
            self.db.query('DROP TABLE `{0}`'.format(site_table_name))

        self.db.query('CREATE TABLE `{0}` LIKE `sites`'.format(site_table_name))

        # Then use insert select join to convert the names to ids
        sql = 'INSERT INTO `{0}` (`site_id`, `status`, `quota`)'.format(site_table_name)
        sql += ' SELECT s.`id`, t.`status`, t.`quota` FROM `{0}`.`{1}` AS t'.format(self.db.scratch_db, tmp_table)
        sql += ' INNER JOIN `{0}`.`sites` AS s ON s.`name` = t.`site`'.format(self.history_db)
        self.db.query(sql)

        self.db.drop_tmp_table(tmp_table)

        ## Now transfer data to an SQLite file
        try:
            cycle_number += 0
        except TypeError:
            # cycle_number is actually the partition name
            db_file_name = '%s/snapshot_%s.db' % (self.snapshots_spool_dir, cycle_number)
            is_cycle = False
        else:
            # Saving quotas during a cycle
            db_file_name = '%s/snapshot_%09d.db' % (self.snapshots_spool_dir, cycle_number)
            is_cycle = True

        try:
            os.makedirs(self.snapshots_spool_dir)
            os.chmod(self.snapshots_spool_dir, 0777)
        except OSError:
            pass

        if os.path.exists(db_file_name):
            os.unlink(db_file_name)

        LOG.info('Creating snapshot SQLite3 DB %s', db_file_name)

        # For some unknown reason, we observed sqlite3.connect hanging - employing alarm mechanism to time out
        class Timeout(Exception):
            pass

        def timeout_handler(signum, frame):
            raise Timeout()

        signal.signal(signal.SIGALRM, timeout_handler)

        signal.alarm(5)

        for att in range(3):
            try:
                snapshot_db = sqlite3.connect(db_file_name)
            except Timeout:
                continue
            else:
                break

        snapshot_cursor = snapshot_db.cursor()

        # Make enum mapping tables
        # Get the decision value to name mapping from MySQL information_schema
        # This is just a fancy way to arrive at a list [(1, 'delete'), (2, 'keep'), (3, 'protect')]
        enum = self.db.query('SELECT `COLUMN_TYPE` FROM `information_schema`.`COLUMNS` WHERE `TABLE_SCHEMA` = %s AND `TABLE_NAME` = \'replicas\' AND `COLUMN_NAME` = \'decision\'', self.cache_db)[0]
        # "enum('delete','keep','protect')" -> ['delete', 'keep', 'protect']
        values = map(lambda s: s.replace("'", '').replace('"', ''), enum[5:-1].split(','))
        decision_mapping = []
        for idec, decision in enumerate(values):
            # MySQL enum starts at 1
            decision_mapping.append((idec + 1, decision))

        sql = 'CREATE TABLE `decisions` ('
        sql += '`id` TINYINT PRIMARY KEY NOT NULL,'
        sql += '`value` TEXT NOT NULL'
        sql += ')'
        snapshot_db.execute(sql)
        for value, name in decision_mapping:
            snapshot_db.execute('INSERT INTO `decisions` VALUES (?, ?)', (value, name))

        sql = 'CREATE TABLE `statuses` ('
        sql += '`id` TINYINT PRIMARY KEY NOT NULL,'
        sql += '`value` TEXT NOT NULL'
        sql += ')'
        snapshot_db.execute(sql)
        snapshot_db.execute('INSERT INTO `statuses` VALUES (%d, \'ready\')' % Site.STAT_READY)
        snapshot_db.execute('INSERT INTO `statuses` VALUES (%d, \'waitroom\')' % Site.STAT_WAITROOM)
        snapshot_db.execute('INSERT INTO `statuses` VALUES (%d, \'morgue\')' % Site.STAT_MORGUE)
        snapshot_db.execute('INSERT INTO `statuses` VALUES (%d, \'unknown\')' % Site.STAT_UNKNOWN)

        # Fill in the replica states
        sql = 'CREATE TABLE `replicas` ('
        sql += '`site_id` SMALLINT NOT NULL,'
        sql += '`dataset_id` INT NOT NULL,'
        sql += '`size` BIGINT NOT NULL,'
        sql += '`decision_id` TINYINT NOT NULL REFERENCES `decisions`(`id`),'
        sql += '`condition` MEDIUMINT NOT NULL'
        sql += ')'
        snapshot_db.execute(sql)
        snapshot_db.execute('CREATE INDEX `site_dataset` ON `replicas` (`site_id`, `dataset_id`)')

        sql = 'INSERT INTO `replicas` VALUES (?, ?, ?, ?, ?)'

        for entry in self.db.xquery('SELECT `site_id`, `dataset_id`, `size`, 0+`decision`, `condition` FROM `{0}`'.format(replica_table_name)):
            snapshot_cursor.execute(sql, entry)

        snapshot_db.commit()

        # Fill in the site states
        sql = 'CREATE TABLE `sites` ('
        sql += '`site_id` SMALLINT PRIMARY KEY NOT NULL,'
        sql += '`status_id` TINYINT NOT NULL REFERENCES `statuses`(`id`),'
        sql += '`quota` INT NOT NULL'
        sql += ')'
        snapshot_db.execute(sql)

        sql = 'INSERT INTO `sites` VALUES (?, ?, ?)'

        for entry in self.db.xquery('SELECT `site_id`, 0+`status`, `quota` FROM `{0}`'.format(site_table_name)):
            snapshot_cursor.execute(sql, entry)

        snapshot_db.commit()

        # Close the sqlite file
        snapshot_cursor.close()
        snapshot_db.close()

        if is_cycle:
            # This was a numbered cycle
            # Archive the sqlite3 file
    
            scycle = '%09d' % cycle_number
            archive_dir_name = '%s/%s/%s' % (self.snapshots_archive_dir, scycle[:3], scycle[3:6])
            xz_file_name = '%s/snapshot_%09d.db.xz' % (archive_dir_name, cycle_number)
    
            try:
                os.makedirs(archive_dir_name)
            except OSError:
                pass
    
            with open(db_file_name, 'rb') as db_file:
                with open(xz_file_name, 'wb') as xz_file:
                    xz_file.write(lzma.compress(db_file.read()))

            self._update_cache_usage('replicas', cycle_number)
            self._update_cache_usage('sites', cycle_number)

        # Finally restore the history DB
        self.db.use_db(self.history_db)

        self.db.reuse_connection = reuse

    def make_cycle_entry(self, cycle_number, site):
        history_record = self.make_entry(site.name)

        if not self._read_only:
            self.db.query('INSERT INTO `cycle_deletion_operations` (`cycle_id`, `operation_id`) VALUES (%s, %s)', cycle_number, history_record.operation_id)

        return history_record
