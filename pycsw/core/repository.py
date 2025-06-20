# -*- coding: utf-8 -*-
# =================================================================
#
# Authors: Tom Kralidis <tomkralidis@gmail.com>
#          Angelos Tzotsos <tzotsos@gmail.com>
#          Ricardo Garcia Silva <ricardo.garcia.silva@gmail.com>
#
# Copyright (c) 2024 Tom Kralidis
# Copyright (c) 2015 Angelos Tzotsos
# Copyright (c) 2017 Ricardo Garcia Silva
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

import inspect
import logging
import os
from time import sleep

from shapely.wkt import loads
from shapely.errors import ShapelyError

from sqlalchemy import create_engine, func, __version__, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql import text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import create_session

from pycsw.core import util
from pycsw.core.etree import etree
from pycsw.core.etree import PARSER

LOGGER = logging.getLogger(__name__)


class Repository(object):
    _engines = {}

    @classmethod
    def create_engine(clazz, url):
        '''
        SQL Alchemy engines are thread-safe and simple wrappers for connection pools

        https://groups.google.com/forum/#!topic/sqlalchemy/t8i3RSKZGb0

        To reduce startup time we can cache the engine as a class variable in the
        repository object and do database initialization once

        Engines are memoized by url
        '''
        if url not in clazz._engines:
            LOGGER.info('creating new engine: %s', util.sanitize_db_connect(url))
            engine = create_engine('%s' % url, echo=False, pool_pre_ping=True)

            # load SQLite query bindings
            # This can be directly bound via events
            # for sqlite < 0.7, we need to to this on a per-connection basis
            if engine.name in ['sqlite', 'sqlite3'] and __version__ >= '0.7':
                from sqlalchemy import event

                @event.listens_for(engine, "connect")
                def connect(dbapi_connection, connection_rec):
                    create_custom_sql_functions(dbapi_connection)

            clazz._engines[url] = engine

        return clazz._engines[url]

    ''' Class to interact with underlying repository '''
    def __init__(self, database, context, app_root=None, table='records', repo_filter=None):
        ''' Initialize repository '''

        self.context = context
        self.filter = repo_filter
        self.fts = False
        self.database = database
        self.table = table

        # Don't use relative paths, this is hack to get around
        # most wsgi restriction...
        if (app_root and self.database.startswith('sqlite:///') and
                not self.database.startswith('sqlite:////')):
            self.database = self.database.replace('sqlite:///', 'sqlite:///%s%s' % (app_root, os.sep))

        self.engine = Repository.create_engine('%s' % self.database)

        base = declarative_base(bind=self.engine)

        LOGGER.info('binding ORM to existing database')

        self.postgis_geometry_column = None

        schema_name, table_name = table.rpartition(".")[::2]

        default_table_args = {
            "autoload": True,
            "schema": schema_name or None
        }
        column_constraints = context.md_core_model.get("column_constraints")

        # Note: according to the sqlalchemy docs available here:
        #
        # https://docs.sqlalchemy.org/en/14/orm/declarative_tables.html#declarative-table-configuration
        #
        # the __table_args__ attribute can either be a tuple or a dict
        if column_constraints is not None:
            table_args = tuple((*column_constraints, default_table_args))
        else:
            table_args = default_table_args

        self.dataset = type(
            'dataset',
            (base,),
            {
                "__tablename__": table_name,
                "__table_args__": table_args
            }
        )

        self.dbtype = self.engine.name

        self.session = create_session(self.engine)
        self.func = func

        temp_dbtype = None

        self.query_mappings = {
            'identifier': self.dataset.identifier,
            'type': self.dataset.type,
            'typename': self.dataset.typename,
            'parentidentifier': self.dataset.parentidentifier,
            'collections': self.dataset.parentidentifier,
            'updated': self.dataset.insert_date,
            'title': self.dataset.title,
            'description': self.dataset.abstract,
            'keywords': self.dataset.keywords,
            'edition': self.dataset.edition,
            'anytext': self.dataset.anytext,
            'bbox': self.dataset.wkt_geometry,
            'date': self.dataset.date,
            'datetime': self.dataset.date,
            'time_begin': self.dataset.time_begin,
            'time_end': self.dataset.time_end,
            'platform': self.dataset.platform,
            'instrument': self.dataset.instrument,
            'sensortype': self.dataset.sensortype,
            'off_nadir': self.dataset.illuminationelevationangle
        }

        if self.dbtype == 'postgresql':
            # check if PostgreSQL is enabled with PostGIS 1.x
            try:
                self.session.execute(select([func.postgis_version()]))
                temp_dbtype = 'postgresql+postgis+wkt'
                LOGGER.debug('PostgreSQL+PostGIS1+WKT detected')
            except Exception:
                LOGGER.exception('PostgreSQL+PostGIS1+WKT detection failed')

            # check if PostgreSQL is enabled with PostGIS 2.x
            try:
                self.session.execute('select(postgis_version())')
                temp_dbtype = 'postgresql+postgis+wkt'
                LOGGER.debug('PostgreSQL+PostGIS2+WKT detected')
            except Exception:
                LOGGER.exception('PostgreSQL+PostGIS2+WKT detection failed')

            # check if a native PostGIS geometry column exists
            try:
                result = self.session.execute(
                    "select f_geometry_column "
                    "from geometry_columns "
                    "where f_table_name = '%s' "
                    "and f_geometry_column != 'wkt_geometry' "
                    "limit 1;" % table_name
                )
                row = result.fetchone()
                self.postgis_geometry_column = str(row['f_geometry_column'])
                temp_dbtype = 'postgresql+postgis+native'
                LOGGER.debug('PostgreSQL+PostGIS+Native detected')
            except Exception:
                LOGGER.exception('PostgreSQL+PostGIS+Native not picked up: %s', table_name)

            # check if a native PostgreSQL FTS GIN index exists
            result = self.session.execute("select relname from pg_class where relname='fts_gin_idx'").scalar()
            self.fts = bool(result)
            LOGGER.debug('PostgreSQL FTS enabled: %r', self.fts)

        if temp_dbtype is not None:
            LOGGER.debug('%s support detected', temp_dbtype)
            self.dbtype = temp_dbtype

        if self.dbtype == 'postgresql+postgis+native':
            LOGGER.debug('Adjusting to PostGIS geometry column  (wkb_geometry)')
            self.query_mappings['bbox'] = self.dataset.wkb_geometry

        if self.dbtype in ['sqlite', 'sqlite3']:  # load SQLite query bindings
            # <= 0.6 behaviour
            if not __version__ >= '0.7':
                self.connection = self.engine.raw_connection()
                create_custom_sql_functions(self.connection)

        LOGGER.info('setting repository queryables')
        # generate core queryables db and obj bindings
        self.queryables = {}
        for tname in self.context.model['typenames']:
            for qname in self.context.model['typenames'][tname]['queryables']:
                self.queryables[qname] = {}

                for qkey, qvalue in \
                        self.context.model['typenames'][tname]['queryables'][qname].items():
                    self.queryables[qname][qkey] = qvalue

        # flatten all queryables
        # TODO smarter way of doing this
        self.queryables['_all'] = {}
        for qbl in self.queryables:
            if qbl != '_all':
                self.queryables['_all'].update(self.queryables[qbl])

        self.queryables['_all'].update(self.context.md_core_model['mappings'])

    def ping(self, max_tries=10, wait_seconds=10):
        LOGGER.debug(f"Waiting for {util.sanitize_db_connect(self.database)}...")

        if self.database.startswith('sqlite'):
            sql = 'SELECT sqlite_version();'
        else:
            sql = 'SELECT version();'

        engine = create_engine(self.database)
        current_try = 0
        while current_try < max_tries:
            try:
                engine.execute(sql)
                LOGGER.debug("Database is already up!")
                break
            except OperationalError as err:
                LOGGER.debug(f"Database not responding yet {err}")
                current_try += 1
                sleep(wait_seconds)
        else:
            raise RuntimeError(
                f"Database not responding at {util.sanitize_db_connect(self.database)} after {max_tries} tries. ")

    def rebuild_db_indexes(self):
        """Rebuild database indexes"""

        LOGGER.info('Rebuilding database %s, table %s', util.sanitize_db_connect(self.database), self.table)
        connection = self.engine.connect()
        connection.autocommit = True
        connection.execute('REINDEX %s' % self.table)
        connection.close()
        LOGGER.info('Done')

    def optimize_db(self):
        """Optimize database"""
        from sqlalchemy.exc import ArgumentError, OperationalError

        LOGGER.info('Optimizing database %s', util.sanitize_db_connect(self.database))
        connection = self.engine.connect()
        try:
            # PostgreSQL
            connection.execution_options(isolation_level="AUTOCOMMIT")
            connection.execute('VACUUM ANALYZE')
        except (ArgumentError, OperationalError):
            # SQLite
            connection.autocommit = True
            connection.execute('VACUUM')
            connection.execute('ANALYZE')
        finally:
            connection.close()
            LOGGER.info('Done')

    def _create_values(self, values):
        value_dict = {}
        for num, value in enumerate(values):
            value_dict['pvalue%d' % num] = value
        return value_dict

    def describe(self):
        ''' Derive table columns and types '''

        type_mappings = {
            'TEXT': 'string',
            'VARCHAR': 'string'
        }

        properties = {
            'geometry': {
                '$ref': 'https://geojson.org/schema/Polygon.json',
                'x-ogc-role': 'primary-geometry'
            }
        }

        for i in self.dataset.__table__.columns:
            if i.name in ['anytext', 'metadata', 'metadata_type', 'xml']:
                continue

            properties[i.name] = {
                'title': i.name
            }

            if i.name == 'identifier':
                properties[i.name]['x-ogc-role'] = 'id'

            try:
                properties[i.name]['type'] = type_mappings[str(i.type)]
            except Exception as err:
                LOGGER.debug(f'Cannot determine type: {err}')

        return properties

    def query_ids(self, ids):
        ''' Query by list of identifiers '''

        column = getattr(self.dataset,
                         self.context.md_core_model['mappings']['pycsw:Identifier'])

        query = self.session.query(self.dataset).filter(column.in_(ids))
        return self._get_repo_filter(query).all()

    def query_collections(self, filters=None, limit=10):
        ''' Query for parent collections '''

        column = getattr(self.dataset,
                         self.context.md_core_model['mappings']['pycsw:ParentIdentifier'])

        collections = self.session.query(column).distinct()

        results = self._get_repo_filter(collections).all()

        ids = [res[0] for res in results if res[0] is not None]

        column = getattr(self.dataset,
                         self.context.md_core_model['mappings']['pycsw:Identifier'])

        query = self.session.query(self.dataset).filter(column.in_(ids))

        collection_typenames = [
            'stac:Collection'
        ]

        column = getattr(self.dataset,
                         self.context.md_core_model['mappings']['pycsw:Typename'])

        query2 = self.session.query(self.dataset).filter(column.in_(collection_typenames))

        if filters is not None:
            LOGGER.debug('Querying repository with additional filters')
            return list(set(self._get_repo_filter(query).filter(filters).limit(limit).all()) |
                        set(self._get_repo_filter(query2).filter(filters).limit(limit).all()))

        return list(set(self._get_repo_filter(query).limit(limit).all()) |
                    set(self._get_repo_filter(query2).limit(limit).all()))

    def query_domain(self, domain, typenames, domainquerytype='list',
                     count=False):
        ''' Query by property domain values '''

        domain_value = getattr(self.dataset, domain)

        if domainquerytype == 'range':
            LOGGER.info('Generating property name range values')
            query = self.session.query(func.min(domain_value),
                                       func.max(domain_value))
        else:
            if count:
                LOGGER.info('Generating property name frequency counts')
                query = self.session.query(getattr(self.dataset, domain),
                                           func.count(domain_value)).group_by(domain_value)
            else:
                query = self.session.query(domain_value).distinct()
        return self._get_repo_filter(query).all()

    def query_insert(self, direction='max'):
        ''' Query to get latest (default) or earliest update to repository '''
        column = getattr(self.dataset,
                         self.context.md_core_model['mappings']['pycsw:InsertDate'])

        if direction == 'min':
            return self._get_repo_filter(self.session.query(func.min(column))).first()[0]
        # else default max
        return self._get_repo_filter(self.session.query(func.max(column))).first()[0]

    def query_source(self, source):
        ''' Query by source '''
        column = getattr(self.dataset,
                         self.context.md_core_model['mappings']['pycsw:Source'])

        query = self.session.query(self.dataset).filter(column == source)
        return self._get_repo_filter(query).all()

    def query(self, constraint, sortby=None, typenames=None,
              maxrecords=10, startposition=0):
        ''' Query records from underlying repository '''

        # run the raw query and get total
        if 'where' in constraint:  # GetRecords with constraint
            LOGGER.debug('constraint detected')
            query = self.session.query(self.dataset).filter(
                                       text(constraint['where'])).params(self._create_values(constraint['values']))
        else:  # GetRecords sans constraint
            LOGGER.debug('No constraint detected')
            query = self.session.query(self.dataset)

        total = self._get_repo_filter(query).count()

        if util.ranking_pass:  # apply spatial ranking
            # TODO: Check here for dbtype so to extract wkt from postgis native to wkt
            LOGGER.debug('spatial ranking detected')
            LOGGER.debug('Target WKT: %s', getattr(self.dataset, self.context.md_core_model['mappings']['pycsw:BoundingBox']))
            LOGGER.debug('Query WKT: %s', util.ranking_query_geometry)
            query = query.order_by(func.get_spatial_overlay_rank(getattr(self.dataset, self.context.md_core_model['mappings']['pycsw:BoundingBox']), util.ranking_query_geometry).desc())
            # trying to make this wsgi safe
            util.ranking_pass = False
            util.ranking_query_geometry = ''

        if sortby is not None:  # apply sorting
            LOGGER.debug('sorting detected')
            # TODO: Check here for dbtype so to extract wkt from postgis native to wkt
            sortby_column = getattr(self.dataset, sortby['propertyname'])

            if sortby['order'] == 'DESC':  # descending sort
                if 'spatial' in sortby and sortby['spatial']:  # spatial sort
                    query = query.order_by(func.get_geometry_area(sortby_column).desc())
                else:  # aspatial sort
                    query = query.order_by(sortby_column.desc())
            else:  # ascending sort
                if 'spatial' in sortby and sortby['spatial']:  # spatial sort
                    query = query.order_by(func.get_geometry_area(sortby_column))
                else:  # aspatial sort
                    query = query.order_by(sortby_column)

        # always apply limit and offset
        return [str(total), self._get_repo_filter(query).limit(
            maxrecords).offset(startposition).all()]

    def insert(self, record, source, insert_date):
        ''' Insert a record into the repository '''

        if isinstance(record.xml, bytes):
            LOGGER.debug('Decoding bytes to unicode')
            record.xml = record.xml.decode()

        try:
            self.session.begin()
            self.session.add(record)
            self.session.commit()
        except Exception as err:
            LOGGER.exception(err)
            self.session.rollback()
            raise

    def update(self, record=None, recprops=None, constraint=None):
        ''' Update a record in the repository based on identifier '''

        if record is not None:
            identifier = getattr(record,
                                 self.context.md_core_model['mappings']['pycsw:Identifier'])

            if isinstance(record.xml, bytes):
                LOGGER.debug('Decoding bytes to unicode')
                record.xml = record.xml.decode()

        if recprops is None and constraint is None:  # full update
            LOGGER.debug('full update')
            update_dict = dict([(getattr(self.dataset, key),
                                 getattr(record, key))
                               for key in record.__dict__.keys() if key != '_sa_instance_state'])

            try:
                self.session.begin()
                self._get_repo_filter(self.session.query(self.dataset)).filter_by(
                    identifier=identifier).update(update_dict, synchronize_session='fetch')
                self.session.commit()
            except Exception as err:
                self.session.rollback()
                msg = 'Cannot commit to repository'
                LOGGER.exception(msg)
                raise RuntimeError(msg) from err
        else:  # update based on record properties
            LOGGER.debug('property based update')
            try:
                rows = rows2 = 0
                self.session.begin()
                for rpu in recprops:
                    # update queryable column and XML document via XPath
                    if 'xpath' not in rpu['rp']:
                        self.session.rollback()
                        raise RuntimeError('XPath not found for property %s' % rpu['rp']['name'])
                    if 'dbcol' not in rpu['rp']:
                        self.session.rollback()
                        raise RuntimeError('property not found for XPath %s' % rpu['rp']['name'])
                    rows += self._get_repo_filter(self.session.query(self.dataset)).filter(
                        text(constraint['where'])).params(self._create_values(constraint['values'])).update({
                            getattr(self.dataset, rpu['rp']['dbcol']): rpu['value'],
                            'xml': func.update_xpath(str(self.context.namespaces),
                                                     getattr(self.dataset, self.context.md_core_model['mappings']['pycsw:XML']), str(rpu)),
                            }, synchronize_session='fetch')
                    # then update anytext tokens
                    rows2 += self._get_repo_filter(self.session.query(self.dataset)).filter(
                        text(constraint['where'])).params(self._create_values(constraint['values'])).update({
                            'anytext': func.get_anytext(getattr(
                                self.dataset, self.context.md_core_model['mappings']['pycsw:XML']))
                        }, synchronize_session='fetch')
                self.session.commit()
                LOGGER.debug('Updated %d records', rows)
                return rows
            except Exception as err:
                self.session.rollback()
                msg = 'Cannot commit to repository'
                LOGGER.exception(msg)
                raise RuntimeError(msg) from err

    def delete(self, constraint):
        ''' Delete a record from the repository '''

        LOGGER.debug('Deleting record with constraint: %s', constraint)
        try:
            self.session.begin()
            rows = self._get_repo_filter(self.session.query(self.dataset)).filter(
                text(constraint['where'])).params(self._create_values(constraint['values']))

            parentids = []
            for row in rows:  # get ids
                parentids.append(getattr(row,
                                 self.context.md_core_model['mappings']['pycsw:Identifier']))

            rows = rows.delete(synchronize_session='fetch')

            if rows > 0:
                LOGGER.debug('Deleting all child records')
                # delete any child records which had this record as a parent
                rows += self._get_repo_filter(self.session.query(self.dataset)).filter(
                    getattr(self.dataset,
                            self.context.md_core_model['mappings']['pycsw:ParentIdentifier']).in_(parentids)).delete(
                            synchronize_session='fetch')

            self.session.commit()
            LOGGER.debug('Deleted %d records', rows)
        except Exception as err:
            self.session.rollback()
            msg = 'Cannot commit to repository'
            LOGGER.exception(msg)
            raise RuntimeError(msg) from err

        return rows

    def exists(self):
        if self.database.startswith('sqlite:'):
            db_path = self.database.rpartition(":///")[-1]
            if not os.path.isfile(db_path):
                try:
                    os.makedirs(os.path.dirname(db_path))
                except OSError as exc:
                    if exc.args[0] == 17:  # directory already exists
                        pass

    def _get_repo_filter(self, query):
        ''' Apply repository wide side filter / mask query '''
        if self.filter is not None:
            return query.filter(text(self.filter))
        return query


def create_custom_sql_functions(connection):
    """Register custom functions on the database connection."""

    inspect_function = inspect.getfullargspec

    for function_object in [
        query_spatial,
        update_xpath,
        util.get_anytext,
        get_geometry_area,
        get_spatial_overlay_rank
    ]:
        argspec = inspect_function(function_object)
        connection.create_function(
            function_object.__name__,
            len(argspec.args),
            function_object
        )


def query_spatial(bbox_data_wkt, bbox_input_wkt, predicate, distance):
    """Perform spatial query

    Parameters
    ----------
    bbox_data_wkt: str
        Well-Known Text representation of the data being queried
    bbox_input_wkt: str
        Well-Known Text representation of the input being queried
    predicate: str
        Spatial predicate to use in query
    distance: int or float or str
        Distance parameter for when using either of ``beyond`` or ``dwithin``
        predicates.

    Returns
    -------
    str
        Either ``true`` or ``false`` depending on the result of the spatial
        query

    Raises
    ------
    RuntimeError
        If an invalid predicate is used

    """

    try:
        bbox1 = loads(bbox_data_wkt.split(';')[-1])
        bbox2 = loads(bbox_input_wkt)
        if predicate == 'bbox':
            result = bbox1.intersects(bbox2)
        elif predicate == 'beyond':
            result = bbox1.distance(bbox2) > float(distance)
        elif predicate == 'contains':
            result = bbox1.contains(bbox2)
        elif predicate == 'crosses':
            result = bbox1.crosses(bbox2)
        elif predicate == 'disjoint':
            result = bbox1.disjoint(bbox2)
        elif predicate == 'dwithin':
            result = bbox1.distance(bbox2) <= float(distance)
        elif predicate == 'equals':
            result = bbox1.equals(bbox2)
        elif predicate == 'intersects':
            result = bbox1.intersects(bbox2)
        elif predicate == 'overlaps':
            result = bbox1.intersects(bbox2) and not bbox1.touches(bbox2)
        elif predicate == 'touches':
            result = bbox1.touches(bbox2)
        elif predicate == 'within':
            result = bbox1.within(bbox2)
        else:
            raise RuntimeError(
                'Invalid spatial query predicate: %s' % predicate)
    except (AttributeError, ValueError, ShapelyError, TypeError):
        result = False
    return "true" if result else "false"


def update_xpath(nsmap, xml, recprop):
    """Update XML document XPath values"""

    if isinstance(xml, bytes) or isinstance(xml, str):
        # serialize to lxml
        xml = etree.fromstring(xml, PARSER)

    recprop = eval(recprop)
    nsmap = eval(nsmap)
    try:
        nodes = xml.xpath(recprop['rp']['xpath'], namespaces=nsmap)
        if len(nodes) > 0:  # matches
            for node1 in nodes:
                if node1.text != recprop['value']:  # values differ, update
                    node1.text = recprop['value']
    except Exception as err:
        LOGGER.warning('update_xpath error', exc_info=True)
        raise RuntimeError('ERROR: %s' % str(err)) from err

    return etree.tostring(xml)


def get_geometry_area(geometry):
    """Derive area of a given geometry"""
    try:
        if geometry is not None:
            return str(loads(geometry).area)
        return '0'
    except Exception:
        return '0'


def get_spatial_overlay_rank(target_geometry, query_geometry):
    """Derive spatial overlay rank for geospatial search as per Lanfear (2006)
    http://pubs.usgs.gov/of/2006/1279/2006-1279.pdf"""

    # TODO: Add those parameters to config file
    kt = 1.0
    kq = 1.0
    if target_geometry is not None and query_geometry is not None:
        try:
            q_geom = loads(query_geometry)
            t_geom = loads(target_geometry)
            Q = q_geom.area
            T = t_geom.area
            if any(item == 0.0 for item in [Q, T]):
                LOGGER.warning('Geometry has no area')
                return '0'
            X = t_geom.intersection(q_geom).area
            if kt == 1.0 and kq == 1.0:
                LOGGER.debug('Spatial Rank: %s', str((X/Q)*(X/T)))
                return str((X/Q)*(X/T))
            else:
                LOGGER.debug('Spatial Rank: %s', str(((X/Q)**kq)*((X/T)**kt)))
                return str(((X/Q)**kq)*((X/T)**kt))
        except Exception:
            LOGGER.warning('Cannot derive spatial overlay ranking', exc_info=True)
            return '0'
    return '0'


def setup(database, table, create_sfsql_tables=True, postgis_geometry_column='wkb_geometry',
          extra_columns=[], language='english'):
    """Setup database tables and indexes"""
    from sqlalchemy import Column, create_engine, Integer, MetaData, \
        Table, Text, Unicode
    from sqlalchemy.types import Float
    from sqlalchemy.orm import create_session

    LOGGER.info('Creating database %s', util.sanitize_db_connect(database))
    if database.startswith('sqlite:///'):
        _, filepath = database.split('sqlite:///')
        dirname = os.path.dirname(filepath)
        if not os.path.exists(dirname):
            LOGGER.debug('SQLite directory %s does not exist' % dirname)
            try:
                db_path = database.rpartition(":///")[-1]
                os.makedirs(os.path.dirname(db_path))
            except OSError as exc:
                if exc.args[0] == 17:  # directory already exists
                    LOGGER.debug('Directory already exists')

    dbase = create_engine(database)
    schema_name, table_name = table.rpartition(".")[::2]

    mdata = MetaData(dbase, schema=schema_name or None)
    create_postgis_geometry = False

    # If PostGIS 2.x detected, do not create sfsql tables.
    if dbase.name == 'postgresql':
        try:
            dbsession = create_session(dbase)
            for row in dbsession.execute('select(postgis_lib_version())'):
                postgis_lib_version = row[0]
            create_sfsql_tables = False
            create_postgis_geometry = True
            LOGGER.info('PostGIS %s detected: Skipping SFSQL tables creation', postgis_lib_version)
        except Exception:
            pass

    if create_sfsql_tables:
        LOGGER.info('Creating table spatial_ref_sys')
        srs = Table(
            'spatial_ref_sys', mdata,
            Column('srid', Integer, nullable=False, primary_key=True),
            Column('auth_name', Text),
            Column('auth_srid', Integer),
            Column('srtext', Text)
        )
        srs.create()

        i = srs.insert()
        i.execute(srid=4326, auth_name='EPSG', auth_srid=4326, srtext='GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.01745329251994328,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]')

        LOGGER.info('Creating table geometry_columns')
        geom = Table(
            'geometry_columns', mdata,
            Column('f_table_catalog', Text, nullable=False),
            Column('f_table_schema', Text, nullable=False),
            Column('f_table_name', Text, nullable=False),
            Column('f_geometry_column', Text, nullable=False),
            Column('geometry_type', Integer),
            Column('coord_dimension', Integer),
            Column('srid', Integer, nullable=False),
            Column('geometry_format', Text, nullable=False),
        )
        geom.create()

        i = geom.insert()
        i.execute(f_table_catalog='public', f_table_schema='public',
                  f_table_name=table_name, f_geometry_column='wkt_geometry',
                  geometry_type=3, coord_dimension=2,
                  srid=4326, geometry_format='WKT')

    # abstract metadata information model

    LOGGER.info('Creating table %s', table_name)
    records = Table(
        table_name, mdata,
        # core; nothing happens without these
        Column('identifier', Text, primary_key=True),
        Column('typename', Text,
               default='csw:Record', nullable=False, index=True),
        Column('schema', Text,
               default='http://www.opengis.net/cat/csw/2.0.2', nullable=False,
               index=True),
        Column('mdsource', Text, default='local', nullable=False,
               index=True),
        Column('insert_date', Text, nullable=False, index=True),
        Column('xml', Unicode, nullable=False),
        Column('anytext', Text, nullable=False),
        Column('metadata', Unicode),
        Column('metadata_type', Text, default='application/xml', nullable=False),
        Column('language', Text, index=True),

        # identification
        Column('type', Text, index=True),
        Column('title', Text, index=True),
        Column('title_alternate', Text, index=True),
        Column('abstract', Text, index=True),
        Column('edition', Text, index=True),
        Column('keywords', Text, index=True),
        Column('keywordstype', Text, index=True),
        Column('themes', Text, index=True),
        Column('parentidentifier', Text, index=True),
        Column('relation', Text, index=True),
        Column('time_begin', Text, index=True),
        Column('time_end', Text, index=True),
        Column('topicategory', Text, index=True),
        Column('resourcelanguage', Text, index=True),

        # attribution
        Column('creator', Text, index=True),
        Column('publisher', Text, index=True),
        Column('contributor', Text, index=True),
        Column('organization', Text, index=True),

        # security
        Column('securityconstraints', Text, index=True),
        Column('accessconstraints', Text, index=True),
        Column('otherconstraints', Text, index=True),

        # date
        Column('date', Text, index=True),
        Column('date_revision', Text, index=True),
        Column('date_creation', Text, index=True),
        Column('date_publication', Text, index=True),
        Column('date_modified', Text, index=True),

        Column('format', Text, index=True),
        Column('source', Text, index=True),

        # geospatial
        Column('crs', Text, index=True),
        Column('geodescode', Text, index=True),
        Column('denominator', Text, index=True),
        Column('distancevalue', Text, index=True),
        Column('distanceuom', Text, index=True),
        Column('wkt_geometry', Text),
        Column('vert_extent_min', Float, index=True),
        Column('vert_extent_max', Float, index=True),

        # service
        Column('servicetype', Text, index=True),
        Column('servicetypeversion', Text, index=True),
        Column('operation', Text, index=True),
        Column('couplingtype', Text, index=True),
        Column('operateson', Text, index=True),
        Column('operatesonidentifier', Text, index=True),
        Column('operatesoname', Text, index=True),

        # inspire
        Column('degree', Text, index=True),
        Column('classification', Text, index=True),
        Column('conditionapplyingtoaccessanduse', Text, index=True),
        Column('lineage', Text, index=True),
        Column('responsiblepartyrole', Text, index=True),
        Column('specificationtitle', Text, index=True),
        Column('specificationdate', Text, index=True),
        Column('specificationdatetype', Text, index=True),

        # eo
        Column('platform', Text, index=True),
        Column('instrument', Text, index=True),
        Column('sensortype', Text, index=True),
        Column('cloudcover', Text, index=True),
        # bands: JSON list of dicts with properties: name, units, min, max
        Column('bands', Text, index=True),
        # STAC: view:off_nadir
        Column('illuminationelevationangle', Text, index=True),

        # distribution
        # links: JSON list of dicts with properties: name, description, protocol, url
        Column('links', Text, index=True),
        # contacts: JSON list of dicts with owslib contact properties, name, organization, email, role, etc.
        Column('contacts', Text, index=True),
    )

    # add extra columns that may have been passed via extra_columns
    # extra_columns is a list of sqlalchemy.Column objects
    if extra_columns:
        LOGGER.info('Extra column definitions detected')
        for extra_column in extra_columns:
            LOGGER.info('Adding extra column: %s', extra_column)
            records.append_column(extra_column)

    records.create()

    conn = dbase.connect()

    if dbase.name == 'postgresql':
        LOGGER.info('Creating PostgreSQL Free Text Search (FTS) GIN index')
        tsvector_fts = "alter table %s add column anytext_tsvector tsvector" % table_name
        conn.execute(tsvector_fts)
        index_fts = "create index fts_gin_idx on %s using gin(anytext_tsvector)" % table_name
        conn.execute(index_fts)
        # This needs to run if records exist "UPDATE records SET anytext_tsvector = to_tsvector('english', anytext)"
        trigger_fts = "create trigger ftsupdate before insert or update on %s for each row execute procedure tsvector_update_trigger('anytext_tsvector', 'pg_catalog.%s', 'anytext')" % (table_name, language)
        conn.execute(trigger_fts)

    if dbase.name == 'postgresql' and create_postgis_geometry:
        # create native geometry column within db
        LOGGER.info('Creating native PostGIS geometry column')
        if postgis_lib_version < '2':
            create_column_sql = "SELECT AddGeometryColumn('%s', '%s', 4326, 'POLYGON', 2)" % (table_name, postgis_geometry_column)
        else:
            create_column_sql = "ALTER TABLE %s ADD COLUMN %s geometry(Geometry,4326);" % (table_name, postgis_geometry_column)
        create_insert_update_trigger_sql = '''
DROP TRIGGER IF EXISTS %(table)s_update_geometry ON %(table)s;
DROP FUNCTION IF EXISTS %(table)s_update_geometry();
CREATE FUNCTION %(table)s_update_geometry() RETURNS trigger AS $%(table)s_update_geometry$
BEGIN
    IF NEW.wkt_geometry IS NULL THEN
        RETURN NEW;
    END IF;
    NEW.%(geometry)s := ST_GeomFromText(NEW.wkt_geometry,4326);
    RETURN NEW;
END;
$%(table)s_update_geometry$ LANGUAGE plpgsql;

CREATE TRIGGER %(table)s_update_geometry BEFORE INSERT OR UPDATE ON %(table)s
FOR EACH ROW EXECUTE PROCEDURE %(table)s_update_geometry();
    ''' % {'table': table_name, 'geometry': postgis_geometry_column}

        create_spatial_index_sql = 'CREATE INDEX %(geometry)s_idx ON %(table)s USING GIST (%(geometry)s);' \
            % {'table': table_name, 'geometry': postgis_geometry_column}

        conn.execute(create_column_sql)
        conn.execute(create_insert_update_trigger_sql)
        conn.execute(create_spatial_index_sql)
