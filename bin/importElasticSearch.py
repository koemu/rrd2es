#!/usr/bin/env python
# -*- coding: utf-8 -*-

# ----------------------------------------------
# importElasticSearch.py
# ----------------------------------------------

import sys
sys.path.append( "./lib/" )

from elixir import *
from elasticsearch import Elasticsearch
import yaml
import logging
import logging.config
import os.path
import commands
import json
import re
import time
import datetime

#-----------------------------------------------

class DATA_TEMPLATE_DATA( Entity ):
    using_options( tablename = "data_template_data", autoload = True )

class POLLER_ITEM( Entity ):
    using_options( tablename = "poller_item", autoload = True )

class HOST( Entity ):
    using_options( tablename = "host", autoload = True )


#-----------------------------------------------

def getParam( context, analysis, filename ):
    """
    パラメータを組み立てます
    @param context  設定情報
    @param analysis 分析時に利用する情報を設定します
    @param filename RRDファイルのフルパスを設定します
    @return 組み立てたパラメータの配列を返します
    """
    logging.debug( "START" )
    
    def_params = []
    inverse = analysis.get( "inverse", 0 )
    shift_time = context[ "analysis_range" ] * 24 * 60 * 60

    def_params.append( "DEF:data1_orig=%s:%s:%s" % ( filename, analysis[ "rrdname" ], analysis[ "cf" ] ) )
    def_params.append( "CDEF:data1=data1_orig,%s,*" % analysis.get( "multiple", "1" ) )

    logging.debug( "END" )

    return def_params

#-----------------------------------------------

def getLastUpdate( context, rrdfile ):
    """
    RRDファイルの最終更新日を取得します
    @param context  設定情報
    @param rrdfile  RRDファイルのパス
    @return 最終更新日を返します 0未満はエラーです
    """
    logging.debug( "START" )
    
    xport_params = []
    gen_params = []

    full_param = "%s info %s | grep last_update | awk -F= '{ print  $2; }'" % (
        context[ "rrd_bin_path" ],
        rrdfile
        )
    
    logging.debug( full_param )
    retval = commands.getstatusoutput( full_param )
    if retval[0] != 0:
        logging.error( retval[1] )
        logging.debug( "EXIT" )
        return -1

    logging.debug( "END" )

    return int( retval[1].strip() )

#-----------------------------------------------

def getHistoricalData( context, analysis, params, last_update ):
    """
    RRDToolでデータを取得します
    @param context  設定情報
    @param analysis 分析時に利用する情報を設定します
    @param params   組み立てたパラメータを指定します
    @param last_update 最終更新日を設定します
    @return 成功で0  失敗でそれ以外を返します
    """
    logging.debug( "START" )
    
    xport_params = []
    gen_params = []

    xport_params.append( "--start=%d-%dday" % ( last_update, context[ "analysis_range" ] ) )
    xport_params.append( "--end=%d+%dday" % ( last_update, context[ "analysis_range" ] ) )
    xport_params.append( "--step=300" )
    xport_params.append( "--json" )

    gen_params.append( "XPORT:data1:\"Data\"" )

    full_param = "%s xport %s %s %s" % (
        context[ "rrd_bin_path" ],
        " ".join( xport_params ),
        " ".join( params ),
        " ".join( gen_params )
        )
    
    logging.debug( full_param )
    retval = commands.getstatusoutput( full_param )
    if retval[0] != 0:
        logging.error( retval[1] )
        logging.error( full_param )
        logging.debug( "EXIT" )
        return None

    logging.debug( "END" )

    return json.loads( retval[1].replace( "],", "]", 1 ) )

#-----------------------------------------------

def parseRRDFiles( context, hostlist ):
    """
    RRDFileを解析します
    @param context  設定情報
    @param hostlist ホスト情報
    @return 成功で0 失敗でそれ以外を返します
    """
    logging.debug( "START" )

    records_current = []
    records_predict = []

    # ホスト名一覧
    for host_info in hostlist:
        subrecords_current = []
        subrecords_predict = []
        logging.info( "Predicting host: %s" % host_info[ "hostname" ] )
        # 分析項目一覧
        for analysis_info in context[ "analysis" ]:
            logging.debug( "Analysis: %s" % analysis_info[ "name" ] )

            # 利用するRRDファイルの情報を取得
            rrdfile_infos = []
            for rrdfile_info in host_info[ "rrdfiles" ]:
                if re.match( analysis_info[ "rrdfile" ], rrdfile_info[ "rrdfile" ] ):
                    rrdfile_infos.append( rrdfile_info )
            if len( rrdfile_infos ) == 0:
                # グラフが登録されていない - MySQLやApacheだとあり得る
                logging.debug( "No Information: %s" % host_info[ "hostname" ] )
                continue
            # RRDファイル一覧
            for rrdfile_info in rrdfile_infos:
                records = parseRRDFile( context, rrdfile_info, analysis_info )
                if records is None:
                    continue
                # ElasticSearchに挿入
                graph_name = analysis_info[ "name" ]
                graph_name = graph_name.replace( "<graph_name>", rrdfile_info[ "name" ] )
                graph_name = graph_name.replace( "<host_name>", host_info[ "hostname" ] )
                setRecord( context, host_info[ "hostname" ], graph_name, records )

    logging.debug( "END" )

    return 0

#-----------------------------------------------

def setRecord( context, hostname, graph_name, historical_data ):
    """
    データをElasticSearchに保存します
    @param context         設定情報
    @param hostname        ホスト名
    @param graph_name      グラフ名
    @param historical_data 履歴データ
    @return 成功で0 失敗でそれ以外を返します
    """
    es = Elasticsearch( context[ "es_server" ] )
    step = -1

    info = historical_data[ "meta" ]
    index_name  = "%s" % ( hostname.lower() )
    doctype     = "cacti"

    for record in historical_data[ "data" ]:
        step += 1
        insert_data = {
            "hostname":   hostname,
            "graph_name": graph_name,
            "datetime":   datetime.datetime.fromtimestamp( info[ "start" ] + info[ "step" ] * step ),
            "value":      record[0]
            }
        result = es.index( index = index_name, doc_type = doctype, id = step, body = insert_data )
        logging.debug( result )

#-----------------------------------------------

def parseRRDFile( context, rrdfile_info, analysis_info ):
    """
    1つのRRDファイルをパースします
    """
    logging.debug( "START" )

    archive_file = os.path.basename( rrdfile_info[ "rrdfile" ] )
    rrdfile = os.path.join( context[ "rrd_file_path" ], archive_file )
    logging.debug( "RRD: %s" % rrdfile )

    # RRDファイルの最終更新日を取得
    last_update = getLastUpdate( context, rrdfile )
    if last_update < 0:
        logging.error( "Unable to get last update: %s" % rrdfile )
        return None

    # データを取得
    params = getParam( context, analysis_info, rrdfile )
    historical_data = getHistoricalData( context, analysis_info, params, last_update )
    if historical_data is None:
        logging.error( "Unable to get historical data: %s" % rrdfile )
        logging.debug( "EXIT" )
        return None

    logging.debug( "END" )

    return historical_data

#-----------------------------------------------

def getHostList( context ):
    """
    ホスト一覧を取得します。
    YAMLで設定した内容を基に取得方法が切替ります。
    @param context 設定情報
    @return ホストリスト
    """
    logging.debug( "START" )
    
    new_hostlist   = []
    query_hostlist = HOST.query
    query_hostlist = query_hostlist.order_by( HOST.description )
    cacti_hostlist = query_hostlist.all()

    for hostdata in cacti_hostlist:
        rrdfiles  = []
        record    = {}

        # rrdファイルの情報を取得
        query_rrd = POLLER_ITEM.query
        query_rrd = query_rrd.filter( POLLER_ITEM.host_id == hostdata.id )
        query_rrd = query_rrd.group_by( POLLER_ITEM.rrd_path )
        rrdlist   = query_rrd.all()

        for rrdfile in rrdlist:
            rrdfile_info = {}
            query_data = DATA_TEMPLATE_DATA.query
            query_data = query_data.filter( DATA_TEMPLATE_DATA.local_data_id == rrdfile.local_data_id )
            rrd_data   = query_data.first()
            rrdfile_info[ "rrdfile" ] = rrdfile.rrd_path
            rrdfile_info[ "name" ]    = rrd_data.name_cache
            rrdfiles.append( rrdfile_info )

        record[ "hostname" ]              = hostdata.description
        record[ "rrdfiles" ]              = rrdfiles

        logging.debug( record )
        new_hostlist.append( record )

    logging.debug( "END" )

    return new_hostlist

#-----------------------------------------------
# Main
#-----------------------------------------------

def main():
    """
    Main
    """
    logging.config.fileConfig( "conf/logging.conf" )
    logging.debug( "START" )
  
    argv = sys.argv
    if( len( argv ) < 2 ):
        print "usage " + sys.argv[0] + " YAML_FILENAME"
        sys.exit( -1 )

    raw_yaml = open( sys.argv[1] ).read()
    context = yaml.load( raw_yaml )

    metadata.bind = context[ "db_server_connect" ]
    metadata.bind.echo = False
    setup_all()

    hostlist = getHostList( context )
    parseRRDFiles( context, hostlist )

    session.rollback()
    session.close()

    logging.debug( "END" )

#-----------------------------------------------

if __name__ == '__main__':
    main()
