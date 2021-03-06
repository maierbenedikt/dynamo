import os
import sys
import logging
import time
import re
import multiprocessing
from ConfigParser import ConfigParser

import MySQLdb
import MySQLdb.converters
import MySQLdb.cursors
import MySQLdb.connections

try:
    MySQLdb.converters.quote_tuple
except AttributeError:
    # Old version of MySQLdb - nothing to do
    pass
else:
    # Backward compatibility measure
    # Older versions of MySQLdb has "query = query % db.literal(args)" in cursor.execute, which requires tuples and lists to
    # not convert to string. Newer versions are more sensible and returns a string on all conversions. However, to be backward
    # compatible, we need to break that sensibility.
    from types import TupleType, ListType
    MySQLdb.converters.conversions[TupleType] = MySQLdb.converters.escape_sequence
    MySQLdb.converters.conversions[ListType] = MySQLdb.converters.escape_sequence

from dynamo.dataformat import Configuration

LOG = logging.getLogger(__name__)

class MySQL(object):
    """Generic thread-safe MySQL interface (for an interface)."""

    _default_config = Configuration()
    _default_parameters = {'': {}} # {user: config}

    @staticmethod
    def set_default(config):
        MySQL._default_config = Configuration(config)
        MySQL._default_config.pop('params')

        for user, params in config.params.items():
            MySQL._default_parameters[user] = dict(params)
            MySQL._default_parameters[user]['user'] = user

    @staticmethod
    def escape_string(string):
        """
        String escape without the surrounding quotes.
        """
        return MySQLdb.escape_string(string)

    @staticmethod
    def stringify_sequence(sequence):
        """
        Return a MySQL list string from the sequence.
        """
        return '(%s)' % ','.join(MySQL.escape(sequence))

    @staticmethod
    def escape(value):
        """
        Converts a string to a quoted string and a number to a decimal expression string. Result of the function
        can be used directly as a right-hand-side expression in queries. Lists and tuples are converted to python tuples
        with each element escaped.
        """
        return MySQLdb.escape(value, MySQLdb.converters.conversions)

    class bare(object):
        """
        Pass bare(string) as column values to bypass formatting in insert_get_id (support will be expanded to other methods).
        """
        def __init__(self, value):
            self.value = value

    @staticmethod
    def make_tuple(obj):
        return (obj,)
    
    def __init__(self, config = None):
        config = Configuration(config)

        if 'user' in config:
            user = config.user
        else:
            user = MySQL._default_config.default_user

        try:
            self._connection_parameters = dict(MySQL._default_parameters[user])
        except KeyError:
            self._connection_parameters = {'user': user}

        if 'config_file' in config and 'config_group' in config:
            parser = ConfigParser()
            parser.read(config['config_file'])
            group = config['config_group']
            for ckey, key in [('host', 'host'), ('user', 'user'), ('password', 'passwd'), ('db', 'db')]:
                try:
                    self._connection_parameters[key] = parser.get(group, ckey)
                except:
                    pass

        if 'host' in config:
            self._connection_parameters['host'] = config['host']
        if 'passwd' in config:
            self._connection_parameters['passwd'] = config['passwd']
        if 'db' in config:
            self._connection_parameters['db'] = config['db']

        self._connection = None

        # Avoid interference in case the module is used from multiple threads
        self._connection_lock = multiprocessing.RLock()

        # MySQL tables can be locked by multiple statements but are unlocked with one.
        # In nested functions with each one locking different tables, we need to call UNLOCK TABLES
        # only after the outermost function asks for it.
        self._locked_tables = []
        
        # Use with care! If False, table locks and temporary tables cannot be used
        self.reuse_connection = config.get('reuse_connection', MySQL._default_config.get('reuse_connection', True))

        # Default 1M characters
        self.max_query_len = config.get('max_query_len', MySQL._default_config.get('max_query_len', 1000000))

        # Default database for CREATE TEMPORARY TABLE
        self.scratch_db = config.get('scratch_db', MySQL._default_config.get('scratch_db', ''))

        # Row id of the last insertion. Will be nonzero if the table has an auto-increment primary key.
        # **NOTE** While core execution of query() and xquery() are locked and thread-safe, last_insert_id is not.
        # Use insert_and_get_id() in a threaded environment.
        self.last_insert_id = 0

    def db_name(self):
        return self._connection_parameters['db']

    def use_db(self, db):
        self.close()
        if db is None:
            try:
                self._connection_parameters.pop('db')
            except:
                pass
        else:
            self._connection_parameters['db'] = db

    def hostname(self):
        return self.query('SELECT @@hostname')[0]

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def config(self):
        conf = Configuration()
        for key in ['host', 'user', 'passwd', 'db']:
            try:
                conf[key] = self._connection_parameters[key]
            except KeyError:
                pass
        try:
            conf['config_file'] = self._connection_parameters['read_default_file']
        except KeyError:
            pass
        try:
            conf['config_group'] = self._connection_parameters['read_default_group']
        except KeyError:
            pass

        conf['reuse_connection'] = self.reuse_connection
        conf['max_query_len'] = self.max_query_len
        conf['scratch_db'] = self.scratch_db

        return conf

    def get_cursor(self, cursor_cls = MySQLdb.connections.Connection.default_cursor):
        if self._connection is None:
            self._connection = MySQLdb.connect(**self._connection_parameters)

        return self._connection.cursor(cursor_cls)

    def close_cursor(self, cursor):
        if cursor is not None:
            cursor.close()
    
        if not self.reuse_connection and self._connection is not None:
            self._connection.close()
            self._connection = None

    def query(self, sql, *args, **kwd):
        """
        Execute an SQL query.
        If the query is an INSERT, return the inserted row id (0 if no insertion happened).
        If the query is an UPDATE, return the number of affected rows.
        If the query is a SELECT, return an array of:
         - tuples if multiple columns are called
         - values if one column is called
        """

        try:
            num_attempts = kwd['retries'] + 1
        except KeyError:
            num_attempts = 10

        try:
            silent = kwd['silent']
        except KeyError:
            silent = False

        self._connection_lock.acquire()

        cursor = None
        try:
            cursor = self.get_cursor()
    
            self.last_insert_id = 0

            if LOG.getEffectiveLevel() == logging.DEBUG:
                if len(args) == 0:
                    LOG.debug(sql)
                else:
                    LOG.debug(sql + ' % ' + str(args))
    
            try:
                for _ in range(num_attempts):
                    try:
                        cursor.execute(sql, args)
                        self._connection.commit()
                        break
                    except MySQLdb.OperationalError as err:
                        if not (self.reuse_connection and err.args[0] == 2006):
                            raise
                            #2006 = MySQL server has gone away
                            #If we are reusing connections, this type of error is to be ignored

                        if not silent:
                            LOG.error(str(sys.exc_info()[1]))

                        last_except = sys.exc_info()[1]

                        # reconnect to server
                        cursor.close()
                        self._connection = None
                        cursor = self.get_cursor()
        
                else: # 10 failures
                    if not silent:
                        LOG.error('Too many OperationalErrors. Last exception:')

                    raise last_except
    
            except:
                if not silent:
                    LOG.error('There was an error executing the following statement:')
                    LOG.error(sql[:10000])
                    LOG.error(sys.exc_info()[1])

                raise
    
            result = cursor.fetchall()

            if cursor.description is None:
                # Was an insert, update, or delete query - really? Is there no other way to identify this?
                if cursor.lastrowid != 0:
                    # insert query on an auto-increment column
                    self.last_insert_id = cursor.lastrowid

                self.close_cursor(cursor)
                self._connection_lock.release()

                return cursor.rowcount

            self.close_cursor(cursor)
            self._connection_lock.release()
    
            if len(result) != 0 and len(result[0]) == 1:
                # single column requested
                return [row[0] for row in result]
    
            else:
                return list(result)

        except:
            self.close_cursor(cursor)
            self._fully_unlock()
            raise

    def insert_get_id(self, table, columns = None, values = None, select = None, db = None, **kwd):
        """
        Auto-form an INSERT statement, execute it under a lock and return the last_insert_id.
        @param table    Table name without reverse-quotes.
        @param columns  If an iterable, column names to insert to. If None, insert filling all columns is assumed.
        @param values   If an iterable, column values to insert. Either values or select must be None.
        @param select   If a string must be a full SELECT statement that can insert to the table.
        """

        args = []

        sql = 'INSERT INTO `%s`' % table
        if columns is not None:
            # has to be some iterable
            sql += ' (%s)' % ','.join('`%s`' % c for c in columns)

        if values is not None:
            values_list = []
            for v in values:
                if type(v) is MySQL.bare:
                    # For example, inserting with functions e.g. NOW()
                    values_list.append(v.value)
                else:
                    values_list.append('%s')
                    args.append(v)

            sql += ' VALUES (%s)' % ','.join(values_list)

        elif select is not None:
            sql += ' ' + select

        self._connection_lock.acquire()

        try:
            inserted = self.query(sql, *tuple(args), **kwd)
            if type(inserted) is list:
                raise RuntimeError('Non-insert query executed in insert_get_id')
            elif inserted != 1:
                raise RuntimeError('More than one row inserted in insert_get_id')

            self._connection_lock.release()
            return self.last_insert_id

        except:
            self._fully_unlock()
            raise

    def xquery(self, sql, *args):
        """
        Execute an SQL query. If the query is an INSERT, return the inserted row id (0 if no insertion happened).
        If the query is a SELECT, return an iterator of:
         - tuples if multiple columns are called
         - values if one column is called
        """

        self._connection_lock.acquire()

        cursor = None
        try:
            cursor = self.get_cursor(MySQLdb.cursors.SSCursor)
    
            self.last_insert_id = 0

            if LOG.getEffectiveLevel() == logging.DEBUG:
                if len(args) == 0:
                    LOG.debug(sql)
                else:
                    LOG.debug(sql + ' % ' + str(args))
    
            try:
                for _ in range(10):
                    try:
                        cursor.execute(sql, args)
                        break
                    except MySQLdb.OperationalError:
                        LOG.error(str(sys.exc_info()[1]))
                        last_except = sys.exc_info()[1]
                        # reconnect to server
                        cursor.close()
                        self._connection = None
                        cursor = self.get_cursor(MySQLdb.cursors.SSCursor)
        
                else: # 10 failures
                    LOG.error('Too many OperationalErrors. Last exception:')
                    raise last_except
    
            except:
                LOG.error('There was an error executing the following statement:')
                LOG.error(sql[:10000])
                LOG.error(sys.exc_info()[1])
                raise
    
            if cursor.description is None:
                raise RuntimeError('xquery cannot be used for non-SELECT statements')
    
            row = cursor.fetchone()
            if row is not None:
                single_column = (len(row) == 1)
        
                while row:
                    if single_column:
                        yield row[0]
                    else:
                        yield row
        
                    row = cursor.fetchone()

            self.close_cursor(cursor)
            self._connection_lock.release()

        except:
            self.close_cursor(cursor)
            self._fully_unlock()
            raise

    def execute_many(self, sqlbase, key, pool, additional_conditions = [], order_by = '', on_duplicate_key_update = ''):
        result = []
        result_sum = None

        if type(key) is tuple:
            key_str = '(' + ','.join('`%s`' % k for k in key) + ')'
        elif type(key) is MySQL.bare:
            key_str = key.value
        elif '`' in key or '(' in key:
            # backward compatibility
            key_str = key
        else:
            key_str = '`%s`' % key

        sqlbase += ' WHERE '

        for add in additional_conditions:
            sqlbase += '(%s) AND ' % add

        sqlbase += key_str + ' IN {pool}'

        def execute(pool_expr):
            global result_sum

            sql = sqlbase.format(pool = pool_expr)
            if order_by:
                sql += ' ORDER BY ' + order_by
            if on_duplicate_key_update:
                sql += ' ON DUPLICATE KEY UPDATE ' + on_duplicate_key_update

            vals = self.query(sql)
            if type(vals) is list:
                result.extend(vals)
            elif type(vals) is int:
                if result_sum is None:
                    result_sum = 0

                result_sum += vals

        # executing in batches - we may issue multiple queries
        self._connection_lock.acquire()
        try:
            self._execute_in_batches(execute, pool)
            self._connection_lock.release()
        except:
            self._fully_unlock()
            raise

        if result_sum is None:
            return result
        else:
            return result_sum

    def select_many(self, table, fields, key, pool, additional_conditions = [], order_by = ''):
        sqlbase = self._form_select_many_sql(table, fields)

        return self.execute_many(sqlbase, key, pool, additional_conditions, order_by = order_by)

    def delete_many(self, table, key, pool, additional_conditions = [], db = ''):
        if type(table) is MySQL.bare:
            table_str = table.value
        else:
            table_str = '`%s`' % table

        sqlbase = 'DELETE FROM {table}'.format(table = table_str)

        self.execute_many(sqlbase, key, pool, additional_conditions)

    def insert_many(self, table, fields, mapping, objects, do_update = True, db = '', update_columns = None):
        """
        INSERT INTO table (fields) VALUES (mapping(objects)).
        @param table          Table name.
        @param fields         Name of columns. If None, perform INSERT INTO table VALUES
        @param mapping        Typically a lambda that takes an element in the objects list and return a tuple corresponding to a row to insert.
        @param objects        List or iterator of objects to insert.
        @param do_update      If True, use ON DUPLICATE KEY UPDATE which can be slower than a straight INSERT.
        @param db             DB name.
        @param update_columns Tuple of column names to update when do_update is True. If None, all columns are updated.

        @return  total number of inserted rows.
        """

        try:
            if len(objects) == 0:
                return 0
        except TypeError:
            pass

        # iter() of iterator returns the iterator itself
        itr = iter(objects)

        try:
            # we'll need to have the first element ready below anyway; do it here
            obj = itr.next()
        except StopIteration:
            return 0

        if db == '':
            db = self.db_name()

        sqlbase = 'INSERT INTO `%s`.`%s`' % (db, table)
        if fields:
            sqlbase += ' (%s)' % ','.join('`%s`' % f for f in fields)
        sqlbase += ' VALUES %s'
        if fields and do_update:
            if update_columns is None:
                update_columns = fields

            sqlbase += ' ON DUPLICATE KEY UPDATE ' + ','.join('`{f}`=VALUES(`{f}`)'.format(f = f) for f in update_columns)

        if mapping is None:
            ncol = len(obj)
        else:
            ncol = len(mapping(obj))

        # template = (%s, %s, ...)
        template = '(' + ','.join(['%s'] * ncol) + ')'

        num_inserted = 0

        while True:
            values = ''

            while itr:
                if mapping is None:
                    values += template % MySQL.escape(obj)
                else:
                    values += template % MySQL.escape(mapping(obj))
    
                try:
                    obj = itr.next()
                except StopIteration:
                    itr = None
                    break

                # MySQL allows queries up to 1M characters
                if self.max_query_len > 0 and len(values) > self.max_query_len:
                    break

                values += ','

            if values == '':
                break
            
            num_inserted += self.query(sqlbase % values)

        return num_inserted

    def insert_select_many(self, insert_table, insert_fields, select_table, select_fields, key, pool, do_update = True, db = '', update_columns = None, additional_conditions = [], order_by = ''):
        """
        INSERT INTO insert_table (insert_fields) SELECT select_fields FROM select_table WHERE key IN pool
        @param insert_table   Table to insert to.
        @param insert_fields  Name of columns in insert_table.
        @param select_table   Table to select from.
        @param select_fields  Name of columns in select_table.
        @param key            See select_many.
        @param pool           See select_many.

        @return  total number of inserted rows.
        """

        if db == '':
            db = self.db_name()

        sqlbase = 'INSERT INTO `%s`.`%s`' % (db, insert_table)
        if insert_fields:
            sqlbase += ' (%s)' % ','.join('`%s`' % f for f in insert_fields)

        sqlbase += ' ' + self._form_select_many_sql(select_table, select_fields)
            
        if insert_fields and do_update:
            if update_columns is None:
                update_columns = insert_fields

            update = ','.join('`{f}`=VALUES(`{f}`)'.format(f = f) for f in update_columns)

        num_inserted = self.execute_many(sqlbase, key, pool, additional_conditions = additional_conditions, order_by = order_by, on_duplicate_key_update = update)
        
        return num_inserted

    def insert_update(self, table, fields, *values, **kwd):
        """
        A shortcut function to perform one INSERT ON DUPLICATE KEY UPDATE.
        @param table          Table name
        @param fields         A tuple of field names
        @param values         A tuple of values to insert.
        @param update_columns Optional list of columns to update.
        """

        if 'update_columns' in kwd:
            update_columns = kwd.pop('update_columns')
        else:
            update_columns = fields

        placeholders = ', '.join(['%s'] * len(fields))

        sql = 'INSERT INTO `%s` (' % table
        sql += ', '.join('`%s`' % f for f in fields)
        sql += ') VALUES (' + placeholders + ')'
        sql += ' ON DUPLICATE KEY UPDATE '
        sql += ', '.join('`%s`=VALUES(`%s`)' % (f, f) for f in update_columns)

        return self.query(sql, *values, **kwd)

    def lock_tables(self, read = [], write = [], **kwd):
        """
        Lock tables. Store the list of locked tables.
        @param read   List of table names. A name can be a string (`%s`), 2-tuple (`%s` as %s), or MySQL.bare (%s)
        @param write  Same as read
        """

        if not self.reuse_connection:
            raise RuntimeError('MySQL locks cannot be used when reuse_connection = False.')

        terms = []

        for table in read:
            if type(table) is tuple:
                terms.append(('`%s` AS %s' % table, 'READ'))
            elif type(table) is MySQL.bare:
                terms.append((table.value, 'READ'))
            else:
                terms.append(('`%s`' % table, 'READ'))

        for table in write:
            if type(table) is tuple:
                terms.append(('`%s` AS %s' % table, 'WRITE'))
            elif type(table) is MySQL.bare:
                terms.append((table.value, 'WRITE'))
            else:
                terms.append(('`%s`' % table, 'WRITE'))

        if len(terms) == 0:
            # why was the function even called?
            return

        # acquire thread lock so that other threads don't access the database while table locks are on
        self._connection_lock.acquire()

        try:
            self._locked_tables.append(tuple(terms))
    
            # LOCK TABLES must always have the full list of tables to lock
            # Append the terms to the tables we have already locked so far
            # Need to uniquify the table list + override if there are overlapping READ and WRITE
            all_tables = {}
            for terms in self._locked_tables:
                for term in terms:
                    if term[1] == 'WRITE':
                        all_tables[term[0]] = 'WRITE'
                    elif term[0] not in all_tables:
                        all_tables[term[0]] = term[1]
    
            sql = 'LOCK TABLES ' + ', '.join('%s %s' % term for term in all_tables.iteritems())
            self.query(sql, **kwd)

        except:
            self._fully_unlock()
            raise

    def unlock_tables(self, force = False):
        """
        Unlock all tables if the current lock depth is 1 or force is True.
        """

        try:
            if force:
                del self._locked_tables[:]
            else:
                try:
                    self._locked_tables.pop()
                except IndexError:
                    raise RuntimeError('Call to unlock_tables does not match lock_tables')

            if len(self._locked_tables) == 0:
                self.query('UNLOCK TABLES')

        except:
            self._fully_unlock()
            raise
        else:
            self._connection_lock.release()

    def _form_select_many_sql(self, table, fields):
        if type(fields) is str:
            fields = (fields,)

        quoted = []
        for field in fields:
            if type(field) is MySQL.bare:
                quoted.append(field.value)
            elif '(' in field or '`' in field:
                # backward compatibility
                quoted.append(field)
            else:
                quoted.append('`%s`' % field)

        fields_str = ','.join(quoted)
        
        if type(table) is MySQL.bare:
            table_str = table.value
        else:
            table_str = '`%s`' % table

        return 'SELECT {fields} FROM {table}'.format(fields = fields_str, table = table_str)

    def _execute_in_batches(self, execute, pool):
        """
        Execute the execute function in batches. Pool can be a list or a tuple that defines
        the pool of rows to run execute on.
        """

        if type(pool) is tuple:
            if len(pool) == 2:
                execute('(SELECT `%s` FROM `%s`)' % pool)

            elif len(pool) == 3:
                execute('(SELECT `%s` FROM `%s` WHERE %s)' % pool)

            elif len(pool) == 4:
                # nested pool: the fourth element is the pool argument
                def nested_execute(expr):
                    pool_expr = '(SELECT `%s` FROM `%s` WHERE `%s` IN ' % pool[:3]
                    pool_expr += expr
                    pool_expr += ')'
                    execute(pool_expr)

                self._execute_in_batches(nested_execute, pool[3])

            return

        elif type(pool) is MySQL.bare or type(pool) is str:
            # case str: backward compatibility
            execute(pool)

            return

        # case: container or iterator

        try:
            if len(pool) == 0:
                return
        except TypeError:
            pass

        itr = iter(pool)

        try:
            obj = itr.next()
        except StopIteration:
            # empty set!
            pool_expr = '(NULL)'
            execute(pool_expr)
            return

        # type-checking the element - all elements must share a type
        if type(obj) is tuple or type(obj) is list:
            escape = MySQL.stringify_sequence
        else:
            escape = MySQL.escape

        # need to repeat in case pool is a long list
        while True:
            pool_expr = '('

            while itr:
                # tuples and scalars are all quoted by escape()
                pool_expr += escape(obj)

                try:
                    obj = itr.next()
                except StopIteration:
                    itr = None
                    break

                if self.max_query_len > 0 and len(pool_expr) > self.max_query_len:
                    break

                pool_expr += ','

            if pool_expr == '(':
                break

            pool_expr += ')'

            execute(pool_expr)

    def table_exists(self, table, db = ''):
        if not db:
            db = self.db_name()

        return self.query('SELECT COUNT(*) FROM `information_schema`.`tables` WHERE `table_schema` = %s AND `table_name` = %s', db, table)[0] != 0

    def create_tmp_table(self, table, columns, db = ''):
        """
        Create a temporary table. Can be performed with a CREATE TEMPORARY TABLE privilege (not the full CREATE TABLE).
        @param table    Temporary table name
        @param columns  A list or tuple of column definitions (see make_map for an example). If a string (`X`.`Y` or `Y`), then use LIKE syntax to create.
        @param db       Optional DB name (default is scratch_db).
        """

        if not self.reuse_connection:
            raise RuntimeError('Temporary tables cannot be created when reuse_connection = False.')

        if not db:
            db = self.scratch_db

        if type(columns) is str:
            sql = 'CREATE TEMPORARY TABLE `%s`.`%s` LIKE %s' % (db, table, columns)
        else:
            sql = 'CREATE TEMPORARY TABLE `%s`.`%s` (' % (db, table)
            sql += ','.join(columns)
            sql += ') ENGINE=MyISAM DEFAULT CHARSET=latin1'

        self.query(sql)

    def truncate_tmp_table(self, table, db = ''):
        if not db:
            db = self.scratch_db

        table_full = '`%s`.`%s`' % (db, table)[0][1]
        create_stmt = self.query('SHOW CREATE TABLE %s' % table_full)[0][1]
        self.query('SET sql_notes = 0')
        try:
            self.query('DROP TABLE IF EXISTS ' + table_full)
        except MySQLdb.OperationalError:
            # If executing this line in a lock, we get an op error if the table does not exist.
            pass

        self.query('SET sql_notes = 1')
        self.query(create_stmt)

    def drop_tmp_table(self, table, db = ''):
        if not db:
            db = self.scratch_db

        self.query('SET sql_notes = 0')
        try:
            self.query('DROP TABLE IF EXISTS `%s`.`%s`' % (db, table))
        except MySQLdb.OperationalError:
            # If executing this line in a lock, we get an op error if the table does not exist.
            pass

        self.query('SET sql_notes = 1')

    def make_map(self, table, objects, object_id_map = None, id_object_map = None, key = None, tmp_join = False):
        objitr = iter(objects)

        if tmp_join:
            tmp_table = table + '_map'
            columns = ['`name` varchar(512) CHARACTER SET latin1 COLLATE latin1_general_cs NOT NULL', 'PRIMARY KEY (`name`)']
            self.create_tmp_table(tmp_table, columns)

            # need to create a list first because objects can already be an iterator and iterators can iterate only once
            objlist = list(objitr)
            objitr = iter(objlist)

            if key is None:
                self.insert_many(tmp_table, ('name',), lambda obj: (obj.name,), objlist, db = self.scratch_db)
            else:
                self.insert_many(tmp_table, ('name',), lambda obj: (key(obj),), objlist, db = self.scratch_db)

            name_to_id = dict(self.xquery('SELECT t1.`name`, t1.`id` FROM `%s` AS t1 INNER JOIN `%s`.`%s` AS t2 ON t2.`name` = t1.`name`' % (table, self.scratch_db, tmp_table)))

            self.drop_tmp_table(tmp_table)

        else:
            name_to_id = dict(self.xquery('SELECT `name`, `id` FROM `%s`' % table))

        num_obj = 0
        for obj in objitr:
            num_obj += 1
            try:
                if key is None:
                    obj_id = name_to_id[obj.name]
                else:
                    obj_id = name_to_id[key(obj)]
            except KeyError:
                continue

            if object_id_map is not None:
                object_id_map[obj] = obj_id
            if id_object_map is not None:
                id_object_map[obj_id] = obj

        LOG.debug('make_map %s (%d) obejcts', table, num_obj)

    def _fully_unlock(self):
        # Call when the thread crashed. Fully releases the lock
        while True:
            try:
                self._connection_lock.release()
            except (RuntimeError, AssertionError):
                break
