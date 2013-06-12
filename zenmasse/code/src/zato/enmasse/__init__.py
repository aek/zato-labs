# -*- coding: utf-8 -*-

"""
Copyright (C) 2013 Dariusz Suchojad <dsuch at zato.io>

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
import argparse, logging, os, sys
from contextlib import closing
from copy import deepcopy
from datetime import datetime
from itertools import chain
from json import dumps
from os.path import abspath, exists, join
from traceback import format_exc

# anyjson
from anyjson import loads

# Bunch
from bunch import Bunch, bunchify

# Pip
from pip import download

# Texttable
from texttable import Texttable

# Zato
from zato.cli import ManageCommand, ZATO_INFO_FILE
from zato.cli.check_config import CheckConfig
from zato.cli.zato_command import add_opts, get_parser
from zato.client import AnyServiceInvoker
from zato.common.crypto import CryptoManager
from zato.common.odb.model import ConnDefAMQP, ConnDefWMQ, HTTPBasicAuth, \
     HTTPSOAP, SecurityBase, Server, Service, TechnicalAccount, to_json, WSSDefinition
from zato.common.util import get_config
from zato.server.service import ForceType
from zato.server.service.internal.channel import amqp as channel_amqp_mod
from zato.server.service.internal.channel import jms_wmq as channel_jms_wmq_mod
from zato.server.service.internal.channel import zmq as channel_zmq_mod
from zato.server.service.internal.definition import amqp as definition_amqp_mod
from zato.server.service.internal.definition import jms_wmq as definition_jms_wmq_mod
from zato.server.service.internal import http_soap as http_soap_mod
from zato.server.service.internal.outgoing import amqp as outgoing_amqp_mod
from zato.server.service.internal.outgoing import ftp as outgoing_ftp_mod
from zato.server.service.internal.outgoing import jms_wmq as outgoing_jms_wmq_mod
from zato.server.service.internal.outgoing import sql as outgoing_sql_mod
from zato.server.service.internal.outgoing import zmq as outgoing_zmq_mod
from zato.server.service.internal import scheduler as scheduler_mod
from zato.server.service.internal.security import basic_auth as sec_basic_auth_mod
from zato.server.service.internal.security import tech_account as sec_tech_account_mod
from zato.server.service.internal.security import wss as sec_wss_mod

DEFAULT_COLS_WIDTH = '15,100'
NO_SEC_DEF_NEEDED = 'zato-no-security'

class Code(object):
    def __init__(self, symbol, desc):
        self.symbol = symbol
        self.desc = desc
        
    def __repr__(self):
        return "<{} at {} symbol:'{}' desc:'{}'>".format(
            self.__class__.__name__, hex(id(self)), self.symbol, self.desc)

WARNING_ALREADY_EXISTS_IN_ODB = Code('W01', 'already exists in ODB')
WARNING_MISSING_DEF = Code('W02', 'missing def')
WARNING_NO_DEF_FOUND = Code('W03', 'no def found')
WARNING_MISSING_DEF_INCL_ODB = Code('W04', 'missing def incl. ODB')
ERROR_ITEM_INCLUDED_MULTIPLE_TIMES = Code('E01', 'item incl multiple')
ERROR_ITEM_INCLUDED_BUT_MISSING = Code('E02', 'incl missing')
ERROR_INCLUDE_COULD_NOT_BE_PARSED = Code('E03', 'incl parsing error')
ERROR_NAME_MISSING = Code('E04', 'name missing')
ERROR_DEF_KEY_NOT_DEFINED = Code('E05', 'def key not defined')
ERROR_NO_DEF_KEY_IN_LOOKUP_TABLE = Code('E06', 'no def key in lookup')
ERROR_KEYS_MISSING = Code('E08', 'missing keys')
ERROR_INVALID_SEC_DEF_TYPE = Code('E09', 'invalid sec def type')
ERROR_INVALID_KEY = Code('E10', 'invalid key')
ERROR_SERVICE_NAME_MISSING = Code('E11', 'service name missing')
ERROR_SERVICE_MISSING = Code('E12', 'service missing')
ERROR_COULD_IMPORT_OBJECT = Code('E13', 'could not import object')

class _DummyLink(object):
    """ Pip requires URLs to have a .url attribute.
    """
    def __init__(self, url):
        self.url = url

class _Incorrect(object):
    def __init__(self, value_raw, value, code):
        self.value_raw = value_raw
        self.value = value
        self.code = code
        
    def __repr__(self):
        return "<{} at {} value_raw:'{}' value:'{}' code:'{}'>".format(
            self.__class__.__name__, hex(id(self)), self.value_raw, 
            self.value, self.code)
        
class Warning(_Incorrect):
    pass
        
class Error(_Incorrect):
    pass

class Results(object):
    def __init__(self, warnings, errors):
        self.warnings = warnings
        self.errors = errors

    def _get_ok(self):
        return not(self.warnings or self.errors)
    
    ok = property(_get_ok)
    
class ZatoClient(AnyServiceInvoker):
    def __init__(self, *args, **kwargs):
        super(ZatoClient, self).__init__(*args, **kwargs)
        self.cluster_id = None
        self.odb_session = None
    
class EnMasse(ManageCommand):
    """ Creates server objects en masse.
    """
    # TODO: Ping outgoing connections (at least check ports)
    # TODO: --delete-all-first must never delete Zato stuff
    # TODO: Make sure any new connector-based connections are not active by default

    class SYS_ERROR(ManageCommand.SYS_ERROR):
        NO_INPUT = 11
        CONFLICTING_OPTIONS = 12
        NO_OPTIONS = 13
        INVALID_INPUT = 14
        
    def _on_server(self, args):
        
        self.args = args
        self.curdir = self.args.curdir
        self.replace_odb_objects = self.args.replace_odb_objects
        self.has_import = getattr(args, 'import')
        self.ignore_missing_defs = args.ignore_missing_defs
        self.json = {}
        self.json_to_import = {}
        
        self.odb_objects = Bunch()
        self.odb_services = Bunch()
        
        # 
        # Tasks and scenarios
        #
        # 1) Export all local JSON files into one (--export-local)
        # 2) Export all definitions from ODB (--export-odb)
        # 3) Export all local JSON files with ODB definitions merged into one (--export-local --export-odb):
        # -> 4) Import definitions from a local JSON file (--import)
        #    4a) bail out if local JSON overrides any from ODB (no -f)
        #    4b) override whatever is found in ODB with values from JSON (-f)
        #
        
        if args.export_odb or self.has_import:

            # Checks if connections to ODB/Redis are configured properly
            cc = CheckConfig(self.args)
            cc.show_output = False
            cc.execute(self.args)
    
            # Get client and issue a sanity check as quickly as possible
            self.set_client()
            self.client.invoke('zato.ping')
        
        # Imports and export are mutually excluding
        if self.has_import and (args.export_local or args.export_odb):
            self.logger.error('Cannot specify import and export options at the same time, stopping now')
            sys.exit(self.SYS_ERROR.CONFLICTING_OPTIONS)

        if args.export_local or self.has_import:
            input_path = self.ensure_input_exists()
            self.json = bunchify(loads(open(input_path).read()))
            
            # Local JSON sanity check first
            json_sanity_results = self.json_sanity_check()
            if not json_sanity_results.ok:
                self.logger.error('JSON sanity check failed')        
                self.report_warnings_errors([json_sanity_results])
                sys.exit(self.SYS_ERROR.INVALID_INPUT)
            
        # 3)
        if args.export_local and args.export_odb:
            self.report_warnings_errors(self.export_local_odb())
            self.save_json()
            
        # 1)
        elif args.export_local:
            self.report_warnings_errors(self.export_local())
            self.save_json()
            
        # 2)
        elif args.export_odb:
            self.report_warnings_errors(self.export_odb())
            self.save_json()
           
        # 4) a/b
        elif self.has_import:
            self.report_warnings_errors(self.import_())
            
        else:
            self.logger.error('At least one of --export-local, --export-odb or --import is required, stopping now')
            sys.exit(self.SYS_ERROR.NO_OPTIONS)
        
# ##############################################################################

    def save_json(self):
        now = datetime.now().isoformat() # Not in UTC, we want to use user's TZ
        name = 'zato-export-{}.json'.format(now.replace(':', '_').replace('.', '_'))
        
        f = open(join(self.curdir, name), 'w')
        f.write(dumps(self.json, indent=1, sort_keys=True))
        f.close()
        
        self.logger.info('Data exported to {}'.format(f.name))
        
# ##############################################################################
        
    def set_client(self):
        #
        # TODO: Much of it is copy/pasted from 'zato invoke', this needs to be refactored
        #       before 'zato enmasse' gets into the core.
        #
        
        repo_dir = os.path.join(os.path.abspath(os.path.join(self.args.path)), 'config', 'repo')
        config = get_config(repo_dir, 'server.conf')
        
        priv_key_location = os.path.abspath(os.path.join(repo_dir, config.crypto.priv_key_location))
        
        cm = CryptoManager(priv_key_location=priv_key_location)
        cm.load_keys()
        
        engine_args = Bunch()
        engine_args.odb_type = config.odb.engine
        engine_args.odb_user = config.odb.username
        engine_args.odb_password = cm.decrypt(config.odb.password)
        engine_args.odb_host = config.odb.host
        engine_args.odb_db_name = config.odb.db_name
        
        engine = self._get_engine(engine_args)
        session = self._get_session(engine)
        
        auth = None
        with closing(session) as session:
            cluster = session.query(Server).\
                filter(Server.token == config.main.token).\
                one().cluster
            
            channel = session.query(HTTPSOAP).\
                filter(HTTPSOAP.cluster_id == cluster.id).\
                filter(HTTPSOAP.url_path == '/zato/admin/invoke').\
                one()
            
            if channel.security_id:
                security = session.query(HTTPBasicAuth).\
                    filter(HTTPBasicAuth.id == channel.security_id).\
                    first()
                
                if security:
                    auth = (security.username, security.password)
                    
        self.client = ZatoClient('http://{}'.format(config.main.gunicorn_bind), 
            '/zato/admin/invoke', auth, max_response_repr=15000)
        
        self.client.cluster_id = session.query(Server).\
            filter(Server.token == config.main.token).\
            one().cluster_id
        
        self.client.odb_session = session
        
# ##############################################################################

    def ensure_input_exists(self):
        input_path = abspath(join(self.curdir, self.args.input))
        if not exists(input_path):
            self.logger.error('No such path: [{}]'.format(input_path))

            # TODO: ManageCommand should not ignore exit codes subclasses return
            sys.exit(self.SYS_ERROR.NO_INPUT)
            
        return input_path

# ##############################################################################
    
    def get_warnings_errors(self, items):
        
        warn_idx = 1
        error_idx = 1
        warn_err = {}
        
        for item in items:

            for warning in item.warnings:
                warn_err['warn{:04}/{} {}'.format(warn_idx, warning.code.symbol, warning.code.desc)] = warning.value
                warn_idx += 1
                
            for error in item.errors:
                warn_err['err{:04}/{} {}'.format(error_idx, error.code.symbol, error.code.desc)] = error.value
                error_idx += 1
                
        warn_no = warn_idx-1
        error_no = error_idx-1
                
        return warn_err, warn_no, error_no
            
    def report_warnings_errors(self, items):

        warn_err, warn_no, error_no = self.get_warnings_errors(items)
        table = self.get_table(warn_err)        
        
        warn_plural = '' if warn_no == 1 else 's'
        error_plural = '' if error_no == 1 else 's'
        
        if warn_no or error_no:
            if error_no:
                level = logging.ERROR
            else:
                level = logging.WARN
                
            prefix = '{} warning{} and {} error{} found:\n'.format(warn_no, warn_plural, error_no, error_plural)
            self.logger.log(level, prefix + table.draw())
        
        else:
            # A signal that we found no warnings nor errors
            return True

# ##############################################################################

    def get_table(self, out):
        
        cols_width = self.args.cols_width if self.args.cols_width else DEFAULT_COLS_WIDTH
        cols_width = (elem.strip() for elem in cols_width.split(','))
        cols_width = [int(elem) for elem in cols_width]
        
        table = Texttable()
        table.set_cols_width(cols_width)
        
        # Use text ('t') instead of auto so that boolean values don't get converted into ints
        table.set_cols_dtype(['t', 't']) 
        
        rows = [['Key', 'Value']]
        rows.extend(sorted(out.items()))
        
        table.add_rows(rows)
        
        return table
                
# ##############################################################################

    def get_include_abspath(self, curdir, value):
        return abspath(join(curdir, value.replace('file://', '')))
    
    def is_include(self, value):
        return isinstance(value, basestring)

    def get_json_includes(self):
        for key in sorted(self.json):
            for value in self.json[key]:
                if self.is_include(value):
                    yield key, value
                
    def json_find_include_dups(self):
        seen_includes = {}
        
        for key, value in self.get_json_includes():
            keys = seen_includes.setdefault(value, [])
            keys.append(key)
            
        dups = dict((k,v) for (k,v) in seen_includes.items() if len(v) > 1)
            
        return dups
    
    def json_find_missing_includes(self):
        missing = {}
        for key in sorted(self.json):
            for value in self.json[key]:
                if self.is_include(value):
                    if download.is_file_url(_DummyLink(value)):
                        abs_path = self.get_include_abspath(self.curdir, value)
                        if not exists(abs_path):
                            item = missing.setdefault((value, abs_path), [])
                            item.append(key)
        return missing
    
    def json_find_unparsable_includes(self, missing):
        unparsable = {}
        
        for key in sorted(self.json):
            for value in self.json[key]:
                if self.is_include(value):
                    if download.is_file_url(_DummyLink(value)):
                        abs_path = self.get_include_abspath(self.curdir, value)
                        
                        # No point in parsing what is already known not to exist
                        if abs_path not in missing:
                            try:
                                loads(open(abs_path).read())
                            except Exception, e:
                                exc_pretty = format_exc(e)
                                
                                item = unparsable.setdefault((value, abs_path, exc_pretty), [])
                                item.append(key)

        return unparsable
            
    def json_sanity_check(self):
        errors = []
        
        for raw, keys in sorted(self.json_find_include_dups().items()):
            len_keys = len(keys)
            keys = sorted(set(keys))
            value = '{} included multiple times ({}) \n{}'.format(
                raw, len_keys, '\n'.join(' - {}'.format(name) for name in keys))
            errors.append(Error(raw, value, ERROR_ITEM_INCLUDED_MULTIPLE_TIMES))
            
        missing_items = sorted(self.json_find_missing_includes().items())
        for raw, keys in missing_items:
            missing, missing_abs = raw
            len_keys = len(keys)
            keys = sorted(set(keys))
            value = '{} ({}) missing but needed in multiple definitions ({}) \n{}'.format(
                missing, missing_abs, len_keys, '\n'.join(' - {}'.format(name) for name in keys))
            errors.append(Error(raw, value, ERROR_ITEM_INCLUDED_BUT_MISSING))
            
        unparsable = self.json_find_unparsable_includes([elem[0][1] for elem in missing_items])
        for raw, keys in unparsable.items():
            include, abs_path, exc_pretty = raw
            len_keys = len(keys)
            suffix = '' if len_keys == 1 else 's'
            keys = sorted(set(keys))
            value = '{} ({}) could not be parsed as JSON, used in ({}) definition{}\n{} \n{}'.format(
                include, abs_path, len_keys, suffix, '\n'.join(' - {}'.format(name) for name in keys), exc_pretty)
            errors.append(Error(raw, value, ERROR_INCLUDE_COULD_NOT_BE_PARSED))
            
        return Results([], errors)
    
    def merge_includes(self):
        json_with_includes = Bunch()
        for key, values in self.json.items():
            values_with_includes = json_with_includes.setdefault(key, [])
            for value in values:
                if self.is_include(value):
                    abs_path = self.get_include_abspath(self.curdir, value)
                    include = Bunch(loads(open(abs_path).read()))
                    values_with_includes.append(include)
                else:
                    values_with_includes.append(value)
                    
        self.json = json_with_includes
        self.logger.info('Includes merged in successfully')
        
    def merge_odb_json(self):
        errors = []
        merged = deepcopy(self.odb_objects)
        
        for json_key, json_elems in self.json.items():
            if 'http' in json_key or 'soap' in json_key:
                odb_key = 'http_soap'
            else:
                odb_key = json_key

            if odb_key not in merged:
                sorted_merged = sorted(merged)
                raw = (json_key, odb_key, sorted_merged)
                value = "JSON key '{}' not one of '{}'".format(odb_key, sorted_merged)
                errors.append(Error(raw, value, ERROR_INVALID_KEY))
            else:
                for json_elem in json_elems:
                    if 'http' in json_key or 'soap' in json_key:
                        connection, transport = json_key.split('_', 1)
                        connection = 'outgoing' if connection == 'outconn' else connection
                        
                        for odb_elem in merged.http_soap:
                            if odb_elem.get('transport') == transport and odb_elem.get('connection') == connection:
                                if odb_elem.name == json_elem.name:
                                    merged.http_soap.remove(odb_elem)
                    else:
                        for odb_elem in merged[odb_key]:
                            if odb_elem.name == json_elem.name:
                                merged[odb_key].remove(odb_elem)
                    merged[odb_key].append(json_elem)
                    
        if errors:
            return Results([], errors)
        
        self.json = merged
    
# ##############################################################################

    def get_odb_objects(self):
        
        def _update_service_name(item):
            item.service = self.client.odb_session.query(Service.name).\
                filter(Service.id == item.service_id).one()[0]
        
        def fix_up_odb_object(key, item):
            if key == 'http_soap':
                if item.connection == 'channel':
                    _update_service_name(item)
                if item.security_id:
                    item.sec_def = self.client.odb_session.query(SecurityBase.name).\
                        filter(SecurityBase.id == item.security_id).one()[0]
                else:
                    item.sec_def = NO_SEC_DEF_NEEDED
            elif key == 'scheduler':
                _update_service_name(item)
            elif 'sec_type' in item:
                item['type'] = item['sec_type']
                del item['sec_type']
                
            return item
        
        def get_fields(model):
            return Bunch(loads(to_json(item))[0]['fields'])
        
        self.odb_objects.def_sec = []
        self.odb_objects.def_amqp = []
        self.odb_objects.http_soap = []
        
        basic_auth = self.client.odb_session.query(HTTPBasicAuth).\
            filter(HTTPBasicAuth.cluster_id == self.client.cluster_id)
            
        tech_acc = self.client.odb_session.query(TechnicalAccount).\
            filter(TechnicalAccount.cluster_id == self.client.cluster_id)
        
        wss = self.client.odb_session.query(WSSDefinition).\
            filter(WSSDefinition.cluster_id == self.client.cluster_id)
        
        for query in(basic_auth, tech_acc, wss):
            for item in query.all():
                if not 'zato' in item.name.lower():
                    self.odb_objects.def_sec.append(get_fields(item))
                
        for item in self.client.odb_session.query(ConnDefAMQP).\
            filter(ConnDefAMQP.cluster_id == self.client.cluster_id).all():
            self.odb_objects.def_amqp.append(get_fields(item))
            
        for item in self.client.odb_session.query(HTTPSOAP).\
            filter(HTTPSOAP.cluster_id == self.client.cluster_id).\
            filter(HTTPSOAP.is_internal == False).all():
            self.odb_objects.http_soap.append(get_fields(item))

        service_key = {
            'zato.channel.amqp.get-list':'channel_amqp',
            'zato.channel.jms-wmq.get-list':'channel_jms_wmq',
            'zato.channel.zmq.get-list':'channel_zmq',
            'zato.definition.jms-wmq.get-list':'def_jms_wmq',
            'zato.outgoing.amqp.get-list':'outconn_amqp',
            'zato.outgoing.ftp.get-list':'outconn_ftp',
            'zato.outgoing.jms-wmq.get-list':'outconn_jms_wmq',
            'zato.outgoing.sql.get-list':'outconn_sql',
            'zato.outgoing.zmq.get-list':'outconn_zmq',
            'zato.scheduler.job.get-list':'scheduler',
            }
        
        for value in service_key.values():
            self.odb_objects[value] = []
        
        for service, key in service_key.items():
            response = self.client.invoke(service, {'cluster_id':self.client.cluster_id})
            if response.ok:
                for item in response.data:
                    if not 'zato' in item['name'].lower():
                        self.odb_objects[key].append(Bunch(item))
                    
        for key, items in self.odb_objects.items():
            for item in items:
                fix_up_odb_object(key, item)
                    
# ##############################################################################
    
    def find_already_existing_odb_objects(self):
        warnings = []
        errors = []
        
        def add_warning(key, value_dict, item):
            raw = (key, value_dict)
            msg = '{} already exists in ODB {} ({})'.format(value_dict.toDict(), item.toDict(), key)
            warnings.append(Warning(raw, msg, WARNING_ALREADY_EXISTS_IN_ODB))
        
        for key, values in self.json.items():
            for value_dict in values:
                value_name = value_dict.get('name')
                if not value_name:
                    raw = (key, value_dict)
                    msg = "{} has no 'name' key ({})".format(value_dict.toDict(), key)
                    errors.append(Error(raw, msg, ERROR_NAME_MISSING))

                if key == 'http_soap':
                    connection = value_dict.get('connection')
                    transport = value_dict.get('transport')
                    
                    for item in self.odb_objects.http_soap:
                        if connection == item.connection and transport == item.transport:
                            if value_name == item.name:
                                add_warning(key, value_dict, item)
                else:
                    odb_defs = self.odb_objects[key.replace('-', '_')]
                    for odb_def in odb_defs:
                        if odb_def.name == value_name:
                            add_warning(key, value_dict, odb_def)
                
        return Results(warnings, errors)
    
# ##############################################################################

    def find_missing_defs(self):
        warnings = []
        errors = []
        missing_def_keys = set()
        missing_def_names = {}
        json_keys = tuple(sorted((self.json)))
        
        def _add_error(item,  key_name, def_, json_key):
            raw = (item, def_)
            value = "{} does not define '{}' (value is {}) ({})".format(item.toDict(), key_name, def_, json_key)
            errors.append(Error(raw, value, ERROR_DEF_KEY_NOT_DEFINED))

        defs_keys = {
                'def': ('jms-wmq', 'amqp'),
                'sec_def': ('plain-http', 'soap'),
            }
        
        items_defs = {
            'channel_amqp':'def_amqp',
            'channel_jms_wmq':'def_jms_wmq',
            'channel_plain_http':'def_sec',
            'channel_soap':'def_sec',
            'outconn_amqp':'def_amqp',
            'outconn_jms_wmq':'def_jms_wmq',
            'outconn_plain_http':'def_sec',
            'outconn_soap':'def_sec',
            'http_soap':'def_sec'
        }
        
        _no_sec_needed = ('channel-plain-http', 'channel-soap', 'outconn-plain-http', 'outconn-soap')
        
        def get_needed_defs():
            
            for json_key, json_items in self.json.items():
                for def_name, def_keys in defs_keys.items():
                    for def_key in def_keys:
                        if def_key in json_key:
                            for json_item in json_items:
                                if 'def' in json_key:
                                    continue
                                needed_def = json_item.get(def_key)
                                def_ = json_item.get(def_name)
                                if not def_:
                                    _add_error(json_item, def_name, def_, json_key)
                                yield ({json_key:def_})

        needed_defs = list(get_needed_defs())
        for info_dict in needed_defs:
            item_key, def_name = info_dict.items()[0]
            def_key = items_defs.get(item_key)
            
            if not def_key:
                raw = (info_dict, items_defs)
                value = "Could not find a def key in {} for item_key '{}'".format(items_defs, item_key)
                errors.append(Error(raw, value, ERROR_NO_DEF_KEY_IN_LOOKUP_TABLE))
                
            else:
                defs = self.json.get(def_key)
                if not defs:
                    raw = (def_key, json_keys)
                    if raw in missing_def_keys:
                        continue
                    else:
                        value = "Could not find '{}' definitions among '{}'".format(def_key, json_keys)
                        warnings.append(Warning(raw, value, WARNING_NO_DEF_FOUND))
                        missing_def_keys.add(raw)
                else:
                    for item in defs:
                        if item.get('name') == def_name:
                            break
                    else:
                        if def_name == NO_SEC_DEF_NEEDED and item_key in _no_sec_needed:
                            continue

                        def_names = tuple(sorted([def_.name for def_ in defs]))
                        raw = (def_key, def_name, def_names)
                        dependants = missing_def_names.setdefault(raw, set())
                        dependants.add(item_key)
                        
        if not self.ignore_missing_defs:
            for(def_key, missing_def, existing_ones), dependants in missing_def_names.items():
                if missing_def == NO_SEC_DEF_NEEDED:
                    continue
                dependants = sorted(dependants)
                raw = (def_key, missing_def, existing_ones, dependants)
                value = "'{}' is needed by '{}' but was not among '{}'".format(missing_def, dependants, existing_ones)
                warnings.append(Warning(raw, value, WARNING_MISSING_DEF))
            
        if warnings or errors:
            return Results(warnings, errors)
        
# ##############################################################################

    def validate_input(self):
        errors = []
        required = {}

        create_services = {
            'channel_amqp':channel_amqp_mod.Create,
            'channel_jms_wmq':channel_jms_wmq_mod.Create,
            'channel_plain_http':http_soap_mod.Create,
            'channel_soap':http_soap_mod.Create,
            'channel_zmq':channel_zmq_mod.Create,
            'def_amqp':definition_amqp_mod.Create,
            'def_jms_wmq':definition_jms_wmq_mod.Create,
            'http_soap':http_soap_mod.Create,
            'outconn_amqp':outgoing_amqp_mod.Create,
            'outconn_ftp':outgoing_ftp_mod.Create,
            'outconn_jms_wmq':outgoing_jms_wmq_mod.Create,
            'outconn_plain_http':http_soap_mod.Create,
            'outconn_soap':http_soap_mod.Create,
            'outconn_sql':outgoing_sql_mod.Create,
            'outconn_zmq':outgoing_zmq_mod.Create,
            'scheduler':scheduler_mod.Create,
        }
        
        def_sec_services = {
            'basic_auth':sec_basic_auth_mod.Create,
            'wss':sec_wss_mod.Create,
            'tech_acc':sec_tech_account_mod.Create,
        }
        
        create_services_keys = sorted(create_services)
        def_sec_services_keys = sorted(def_sec_services)
        
        replace_names = {
            'def_id': 'def',
        }
        
        skip_names = ('cluster_id',)
        
        def _needs_password(key):
            return 'sql' in key
        
        for key, service in chain(create_services.items(), def_sec_services.items()):
            required[key] = set()
            for name in service.SimpleIO.input_required:
                if name in skip_names:
                    continue
                if isinstance(name, ForceType):
                    name = name.name
                name = replace_names.get(name, name)
                required[key].add(name)
                
        def _validate(key, item, class_, is_sec):
            name = item.get('name')
            item_dict = item.toDict()
            missing = None
            required_lookup_key = None
            
            if not name:
                raw = (key, item_dict)
                value = "No 'name' key found in item '{}' ({})".format(item_dict, key)
                errors.append(Error(raw, value, ERROR_NAME_MISSING))
            else:
                if is_sec:
                    # We know we have one of correct types already so we can
                    # just look up required attributes.
                    required_keys = required[item.get('type')]
                else:
                    required_keys = required[key]
                    
                if _needs_password(key):
                    required_keys.add('password')
                    
                missing = sorted(required_keys - set(item))
                
                if missing:
                    missing_value = "key '{}'".format(missing[0]) if len(missing) == 1 else "keys '{}'".format(missing)
                    raw = (key, name, item_dict, required_keys, missing)
                    value = "Missing {} in '{}', the rest is '{}' ({})".format(missing_value, name, item_dict, key)
                    errors.append(Error(raw, value, ERROR_KEYS_MISSING))
                    
                # OK, the keys are there, but do they all have non-None values?
                else:
                    for req_key in required_keys:
                        if item.get(req_key) is None: # 0 or '' can be correct values
                            raw = (req_key, required, item_dict, key)
                            value = "Key '{}' must not be None in '{}' ({})".format(req_key, item_dict, key)
        
        for key, items in self.json.items():
            for item in items:
                if key == 'def_sec':
                    sec_type = item.get('type')
                    if not sec_type:
                        item_dict = item.toDict()
                        raw = (key, item_dict)
                        value = "'{}' has no required 'type' key (def_sec) ".format(item_dict)
                        errors.append(Error(raw, value, ERROR_TYPE_MISSING))
                    else:
                        class_ = def_sec_services.get(sec_type)
                        if not class_:
                            raw = (sec_type, def_sec_services_keys, item)
                            value = "Invalid type '{}', must be one of '{}' (def_sec)".format(sec_type, def_sec_services_keys)
                            errors.append(Error(raw, value, ERROR_INVALID_SEC_DEF_TYPE))
                        else:
                            _validate(key, item, class_, True)
                else:
                    class_ = create_services.get(key)
                    if not class_:
                        raw = (key, create_services_keys)
                        value = "Invalid key '{}', must be one of '{}'".format(key, create_services_keys)
                        errors.append(Error(raw, value, ERROR_INVALID_KEY))
                    else:
                        _validate(key, item, class_, False)
                            
        if errors:
            return Results([], errors)
        
# ##############################################################################

    def export(self):
        
        # Find any definitions that are missing
        missing_defs = self.find_missing_defs()
        if missing_defs:
            self.logger.error('Failed to find all definitions needed')        
            return [missing_defs]
        
        # Validate if every required input element has been specified.
        invalid_reqs = self.validate_input()
        if invalid_reqs:
            self.logger.error('Required elements missing')        
            return [invalid_reqs]
        
        return []

    def export_local(self, needs_includes=True):
        if needs_includes:
            self.merge_includes()
        return self.export()
    
    def export_local_odb(self, needs_local=True):
        if needs_local:
            self.merge_includes()
        self.get_odb_objects()
        self.logger.info('ODB objects read')
        
        errors = self.merge_odb_json()
        if errors:
            return [errors]
        self.logger.info('ODB objects merged in')
        
        return self.export_local(False)
    
    def export_odb(self):
        return self.export_local_odb(False)

# ##############################################################################

    def validate_import_data(self):
        warnings = []
        errors = []

        items_defs = {
            'def_amqp':'zato.definition.amqp.get-list',
            'def_jms_wmq':'zato.definition.jms-wmq.get-list',
            'def_sec':'zato.security.get-list',
        }
        
        def has_def(def_type, def_name):
            service = items_defs[def_type]
            response = self.client.invoke(service, {'cluster_id':self.client.cluster_id})
            if response.ok:
                for item in response.data:
                    if item['name'] == def_name:
                        return False
                    
            return False
        
        missing_defs = self.find_missing_defs()
        if missing_defs:
            for warning in missing_defs.warnings:
                def_type, def_name, _, dependants = warning.value_raw
                if not has_def(def_type, def_name):
                    raw = (def_type, def_name)
                    value = "Definition '{}' not found in JSON/ODB ({}), needed by '{}'".format(
                        def_name, def_type, dependants)
                    warnings.append(Warning(raw, value, WARNING_MISSING_DEF_INCL_ODB))

        def needs_service(json_key, item):
            return 'channel' in json_key or json_key == 'scheduler' or \
                   ('http_soap' in json_key and item.get('connection') == 'channel')

        for json_key, items in self.json.items():
            for item in items:
                if needs_service(json_key, item):
                    item_dict = item.toDict()
                    service_name = item.get('service')
                    raw = (service_name, item_dict, json_key)
                    if not service_name:
                        value = "No service defined in '{}' ({})".format(item_dict, json_key)
                        errors.append(Error(raw, value, ERROR_SERVICE_NAME_MISSING))
                    else:
                        if service_name not in self.odb_services:
                            value = "Service '{}' from '{}' missing in ODB ({})".format(service_name, item_dict, json_key)
                            errors.append(Error(raw, value, ERROR_SERVICE_MISSING))

        return Results(warnings, errors)

    def import_objects(self, already_existing):
        warnings = []
        errors = []
        
        existing_defs = []
        existing_other = []
        
        new_defs = []
        new_other = []
        
        # FTP definition may use a password but are not forced to.
        MAYBE_NEEDS_PASSWORD = 'MAYBE_NEEDS_PASSWORD'
        
        self.json_to_import = Bunch(deepcopy(self.json))

        class ImportInfo(object):
            def __init__(self, mod, needs_password=False):
                self.mod = mod
                self.needs_password = needs_password
                
            def __repr__(self):
                return "<{} at {} mod:'{}' needs_password:'{}'>".format(
                    self.__class__.__name__, hex(id(self)), self.mod, self.needs_password)

        service_info = {
            'channel_amqp':ImportInfo(channel_amqp_mod),
            'channel_jms_wmq':ImportInfo(channel_jms_wmq_mod),
            'channel_zmq':ImportInfo(channel_zmq_mod),
            'def_amqp':ImportInfo(definition_amqp_mod, True),
            'def_jms_wmq':ImportInfo(definition_jms_wmq_mod),
            'http_soap':ImportInfo(http_soap_mod),
            'outconn_amqp':ImportInfo(outgoing_amqp_mod),
            'outconn_ftp':ImportInfo(outgoing_ftp_mod, MAYBE_NEEDS_PASSWORD),
            'outconn_jms_wmq':ImportInfo(outgoing_jms_wmq_mod),
            'outconn_sql':ImportInfo(outgoing_sql_mod, True),
            'outconn_zmq':ImportInfo(outgoing_zmq_mod),
            'scheduler':ImportInfo(scheduler_mod),
        }
        
        def_sec_info = {
            'basic_auth':ImportInfo(sec_basic_auth_mod, True),
            'wss':ImportInfo(sec_wss_mod, True),
            'tech_acc':ImportInfo(sec_tech_account_mod, True),
        }
        
        def get_odb_item(item_type, name):
            for item in self.odb_objects[item_type]:
                if item.name == name:
                    return item
                
        def get_security_by_name(name):
            for item in self.odb_objects.def_sec:
                if item.name == name:
                    return item.id
        
        def import_object(def_type, attrs, is_edit):
            attrs_dict = attrs.toDict()
            info_dict, info_key = (def_sec_info, attrs.type) if 'sec' in def_type else (service_info, def_type)
            import_info = info_dict[info_key]
            service_class = getattr(import_info.mod, 'Edit' if is_edit else 'Create')
            
            # Fetch an item from a cache of ODB object and assign its ID
            # to attrs so that the Edit service knows what to update.
            if is_edit:
                odb_item = get_odb_item(def_type, attrs.name)
                attrs.id = odb_item.id
            
            if def_type == 'http_soap':
                if attrs.sec_def == NO_SEC_DEF_NEEDED:
                    attrs.security_id = None
                else:
                    attrs.security_id = get_security_by_name(attrs.sec_def)

            response = self.client.invoke(service_class.get_name(), attrs)
            if not response.ok:
                return response.details
            else:
                verb = 'Updated' if is_edit else 'Created'
                self.logger.info("{} object '{}' ({})".format(verb, attrs.name, item_type))
                if import_info.needs_password:
                    
                    password = attrs.get('password')
                    if not password:
                        if import_info.needs_password == MAYBE_NEEDS_PASSWORD:
                            self.logger.info("Password missing but not required '{}' ({})".format(attrs.name, item_type))
                        else:
                            return "Password missing but is required '{}' ({}) attrs '{}'".format(attrs.name, item_type, attrs_dict)
                    else:
                        if not is_edit:
                            attrs.id = response.data['id']
    
                        service_class = getattr(import_info.mod, 'ChangePassword')
                        request = {'id':attrs.id, 'password1':attrs.password, 'password2':attrs.password}
                        response = self.client.invoke(service_class.get_name(), request)
                        if not response.ok:
                            return response.details
                        else:
                            self.logger.info("Updated password '{}' ({})".format(attrs.name, item_type))
                        
        def remove_from_import_list(item_type, name):
            for json_item_type, items in self.json_to_import.items():
                if json_item_type == item_type:
                    for item in items:
                        if item.name == name:
                            items.remove(item)
                            
                            # Name is unique, we can stop now
                            return
                        
        def _import(item_type, attrs, is_edit):
            attrs_dict = attrs.toDict()
            attrs.cluster_id = self.client.cluster_id
            error_response = import_object(item_type, attrs, is_edit)
            
            # We quit on first error encountered
            if error_response:
                raw = (item_type, attrs_dict, error_response)
                value = "Could not import '{}' with '{}', response was '{}'".format(
                    attrs.name, attrs_dict, error_response)
                errors.append(Error(raw, value, ERROR_COULD_IMPORT_OBJECT))
                return Results(warnings, errors)
            
            # It's been just imported so we don't want to create in next steps
            # (this in fact would result in an error as the object already exists).
            if is_edit:
                remove_from_import_list(item_type, attrs.name)

        #
        # Update already existing objects first, definitions before any object
        # that may depend on them ..
        #
        for w in already_existing.warnings:
            item_type, _ = w.value_raw
            existing = existing_defs if 'def' in item_type else existing_other
            existing.append(w)
        
        #
        # .. actually invoke the updates now ..
        #
        for w in chain(existing_defs, existing_other):
            item_type, attrs = w.value_raw
            results = _import(item_type, attrs, True)
            if results:
                return results
            
        #
        # Create new objects, again, definitions come first ..
        #
        for item_type, items in self.json_to_import.items():
            new = new_defs if 'def' in item_type else new_other
            new.append({item_type:items})
            
        #
        # .. actually create the objects now.
        #
        for elem in chain(new_defs, new_other):
            for item_type, attr_list in elem.items():
                for attrs in attr_list:
                    results = _import(item_type, attrs, False)
                    if results:
                        return results

        return Results(warnings, errors)
        
    def import_(self):
        self.get_odb_objects()
        
        odb_services = self.client.invoke('zato.service.get-list', {'cluster_id':self.client.cluster_id, 'name_filter':'*'})
        if odb_services.has_data:
            for service in odb_services.data:
                self.odb_services[service['name']] = Bunch(service)

        # Find channels and jobs that require services that don't exist
        results = self.validate_import_data()
        if not results.ok:
            return [results]

        already_existing = self.find_already_existing_odb_objects()
        if not already_existing.ok and not self.replace_odb_objects:
            return [already_existing]
        
        results = self.import_objects(already_existing)
        if not results.ok:
            return [results]

        return []

# ##############################################################################

def main():
    parser = argparse.ArgumentParser(add_help=True, description=EnMasse.__doc__)
    parser.add_argument('--store-log', help='Whether to store an execution log', action='store_true')
    parser.add_argument('--verbose', help='Show verbose output', action='store_true')
    parser.add_argument('--store-config', help='Whether to store config options in a file for a later use', action='store_true')
    parser.add_argument('--export-local', help='Export local JSON definitions into one file (can be used with --export-odb)', action='store_true')
    parser.add_argument('--export-odb', help='Export ODB definitions into one file (can be used with --export-local)', action='store_true')
    parser.add_argument('--import', help='Import definitions from a local JSON (excludes --export-*)', action='store_true')
    parser.add_argument('--ignore-missing-defs', help='Import definitions from a local JSON (excludes --export-*)', action='store_true')
    parser.add_argument('--replace-odb-objects', help='Force replacing objects already existing in ODB during import', action='store_true')
    parser.add_argument('--input', help="Path to an input JSON document")
    parser.add_argument('--cols_width', help='A list of columns width to use for the table output, default: {}'.format(DEFAULT_COLS_WIDTH))
    parser.add_argument('--path', help='Path to a running Zato server')
    
    add_opts(parser, EnMasse.opts)

    args = parser.parse_args()
    args.curdir = abspath(os.getcwd())
    
    EnMasse(args).run(args)

if __name__ == '__main__':
    print('TODO document that security def names must be unique')
    print('TODO document zato-no-security constant')
    main()