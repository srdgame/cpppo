from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import ast
import errno
import logging
import os
import re
import struct
import sys
import time

import pytest

if __name__ == "__main__":
    if __package__ is None:
        __package__	= "cpppo.server.enip"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    from cpppo.automata import log_cfg
    logging.basicConfig( **log_cfg )
    logging.getLogger().setLevel( logging.NORMAL )

import cpppo
from cpppo.misc import timer, near, hexdump
from cpppo.modbus_test import nonblocking_command
from cpppo.server import enip
from cpppo.server.enip import client

from cpppo.server.enip.hart import HART, proxy_hart # Class, proxy

log				= logging.getLogger( "HART" )

def start_hart_simulator( *options, **kwds ):
    """Start a simple EtherNet/IP CIP simulator w/ a HART I/O module (execute this file as __main__),
    optionally with Tag=<type>[<size>] (or other) positional arguments appended to the command-line.
    Return the command-line used, and the detected (host,port) address bound.  Looks for something
    like:

        11-11 11:46:16.301     7fff7a619000 network  NORMAL   server_mai enip_srv server PID [ 7573] running on ('', 44818)

    containing a repr of the (<host>,<port>) tuple.  Recover this address using the safe ast.literal_eval.

    At least one positional parameter containing a Tag=<type>[<size>] must be provided.

    """
    command                     = nonblocking_command( [
        'python',
        os.path.abspath( __file__ ),
        '-v',
    ] + list( options ))

    # For python 2/3 compatibility (can't mix positional wildcard, keyword parameters in Python 2)
    CMD_WAIT			= kwds.pop( 'CMD_WAIT', 10.0 )
    CMD_LATENCY			= kwds.pop( 'CMD_LATENCY', 0.1 )
    assert not kwds, "Unrecognized keyword parameter: %s" % ( ", ".join( kwds ))

    begun			= timer()
    address			= None
    data			= ''
    while address is None and timer() - begun < CMD_WAIT:
        # On Python2, socket will raise IOError/EAGAIN; on Python3 may return None 'til command started.
        try:
            raw			= command.stdout.read()
            log.debug( "Socket received: %r", raw)
            if raw:
                data  	       += raw.decode( 'utf-8' )
        except IOError as exc:
            log.debug( "Socket blocking...")
            assert exc.errno == errno.EAGAIN, "Expected only Non-blocking IOError"
        except Exception as exc:
            log.warning("Socket read return Exception: %s", exc)
        if not data:
            time.sleep( CMD_LATENCY )
        while data.find( '\n' ) >= 0:
            line,data		= data.split( '\n', 1 )
            log.info( "%s", line )
            m			= re.search( "running on (\([^)]*\))", line )
            if m:
                address		= ast.literal_eval( m.group(1).strip() )
                log.normal( "EtherNet/IP CIP Simulator started after %7.3fs on %s:%d",
                                    timer() - begun, address[0], address[1] )
                break
    return command,address


@pytest.fixture( scope="module" )
def simulated_hart_gateway():
    return start_hart_simulator()


def test_hart_packet():
    """
    0000   00 1d 9c c9 3e 2a b0 5a da b4 f9 1f 08 00 45 00
    0010   00 66 30 3f 40 00 80 06 00 00 64 64 66 01 64 64
    0020   66 0a c4 50 af 12 f7 0d 5f c5 44 a0 85 e2 50 18
    0030   00 fb 95 2c 00 00 6f 00 26 00 04 00 31 00 00 00
    0040   00 00 6c 74 00 00 88 f9 59 07 00 00 00 00 00 00
    0050   00 00 08 00 02 00 00 00 00 00 b2 00 16 00 52 02
    0060   20 06 24 01 05 f7 08 00 4b 03 21 00 5d 03 24 08
    0070   01 00 01 02
    """

hart_kwds			= dict(
    timeout		= 15.0,
    depth		= 5,		# max. requests in-flight
    multiple		= 0,		# max. bytes of req/rpy per Multiple Service Packet
)

def test_hart_simple( simulated_hart_gateway ):
    # No Multiple Service Packet supported by HART I/O Card simulator

    #logging.getLogger().setLevel( logging.DETAIL )
    command,address             = simulated_hart_gateway

    #address			= ("100.100.102.10", 44818)
    route_path			= None
    route_path			= [{'link': 2, 'port': 1}]
    try:
        assert address, "Unable to detect HART EtherNet/IP CIP Gateway IP address"
        #hio			= client.connector( host=address[0], port=address[1] )
        # Establish an Implicit EtherNet/IP CIP connection using Forward Open
        hio			= client.implicit( host=address[0], port=address[1], connection_path=None )
        PV			= 1.23
        operations		= [
            'HART_7_Data.PV = (REAL)0', # may fail 'til first HART Read Dynamic Variable is done
            {
                "method":	"service_code",
                "code":		HART.RD_VAR_REQ,
                "data":		[],			# No payload
                "data_size":	2+36,			# Known response size: command,status,<payload>
                "path":		'@0x%X/8' % ( HART.class_id ), # Instance 1-8 ==> HART Channel 0-7
            },
            'HART_7_Data.PV = (REAL)%s' % PV,
            {
                "method":	"service_code",
                "code":		HART.RD_VAR_REQ,
                "data":		[],			# No payload
                "data_size":	2+36,			# Known response size: command,status,<payload>
                "path":		'@0x%X/8' % ( HART.class_id ), # Instance 1-8 ==> HART Channel 0-7
            },
        ]

        # Now, use the underlying client.connector to issue a HART "Read Dynamic Variable" Service Code
        with hio:
            results		= []
            failures		= 0
            for idx,dsc,req,rpy,sts,val in hio.pipeline(
                    operations=client.parse_operations( operations, route_path=route_path ), **hart_kwds ):
                log.normal( "Client %s: %s --> %r: %s", hio, dsc, val, enip.enip_format( rpy ))
                if not val:
                    log.warning( "Client %s harvested %d/%d results; failed request: %s",
                                     hio, len( results ), len( operations ), rpy )
                    failures   += 1
                results.append( (dsc,val,rpy) )
            rpylast	       	= results[-1][-1]
            assert failures in (0,1)
            assert near( rpylast.read_var.PV, PV )

    except Exception as exc:
        log.warning( "Test terminated with exception: %s", exc )
        raise


def test_hart_pass_thru_simulated( simulated_hart_gateway ):
    """Simulated HART I/O card; always returns Pass-thru Init handle 99 (won't work on a real device)"""
    #logging.getLogger().setLevel( logging.INFO )
    command,address             = simulated_hart_gateway

    try:
        assert address, "Unable to detect HART EtherNet/IP CIP Gateway IP address"
        hio				= client.connector( host=address[0], port=address[1] )

        operations		= [
            {
                "method":	"service_code",
                "code":		HART.PT_INI_REQ,
                "data":		[1, 0],			# HART: Read primary variable
                "data_size":	2+2,			# Known response size: command,status,<payload>
                "path":		'@0x%X/8' % ( HART.class_id ), # Instance 1-8 ==> HART Channel 0-7
            },
            {
                "method":	"service_code",
                "code":		HART.PT_QRY_REQ,
                "data":		[99],			# HART: Pass-thru Query handle
                "data_size":	2+5,			# Known response size: 5 (units + 4-byte real in network order)
                "path":		'@0x%X/8' % ( HART.class_id ), # Instance 1-8 ==> HART Channel 0-7
            },
        ]

        # Now, use the underlying client.connector to issue a HART "Read Dynamic Variable" Service Code
        with hio:
            results		= []
            failures		= 0
            for idx,dsc,req,rpy,sts,val in hio.pipeline(
                    operations=client.parse_operations( operations ), **hart_kwds ):
                log.normal( "Client %s: %s --> %r: %s", hio, dsc, val, enip.enip_format( rpy ))
                if not val:
                    log.warning( "Client %s harvested %d/%d results; failed request: %s",
                                     hio, len( results ), len( operations ), rpy )
                    failures   += 1
                results.append( (dsc,val,rpy) )
            # assert failures == 0 # statuses represent HART I/O status, not CIP response status
            assert results[0][-1].status in ( 32, 33, 35 )	# 32 busy, 33 initiated, 35 device offline
            assert results[1][-1].status in ( 0, 34, 35 )	# 0 success, 34 running, 35 dead

    except Exception as exc:
        log.warning( "Test terminated with exception: %s", exc )
        raise


def hart_pass_thru( io, path, hart_data, data_size, route_path=None ):
    """For eg. hart_data=[1, 0], data_size=4 for HART command 1.  Returns None on failure, or the HART
    command response data payload.
    
    Harvests a Pass-thru Init handle, and issues Query on it 'til successs.

    """
    # Try to start the Pass-thru "Read primary variable", and get handle
    operations		= [
        {
            "method":		"service_code",
            "code":		HART.PT_INI_REQ,
            "data":		hart_data,
            "data_size":	2+2,			# Known response size: command,status,<payload>
            "path":		path,			# Instance 1-8 ==> HART Channel 0-7
            "route_path":	route_path,
        },
    ]

    # Look for a reply status of 33 initiated. Actually, it appears that status 0 indicates success.
    handle			= None
    while handle is None:
        time.sleep( .1 )
        with io:
            for idx,dsc,req,rpy,sts,val in io.pipeline(
                    operations=client.parse_operations( operations ), **hart_kwds ):
                log.normal( "Client %s: %s --> %r: request: %s\nreply:%s", io, dsc, val,
                            enip.enip_format( req ), enip.enip_format( rpy ))
                if rpy.status in (0, 33):		# 32 busy, 33 initiated, 35 device offline
                    handle	= rpy.init.handle
    log.normal( "HART Pass-thru command Handle: %s", handle )

    # Query for success/failure (loop on running)
    operations		= [
        {
            "method":		"service_code",
            "code":		HART.PT_QRY_REQ,
            "data":		[ handle ],		# HART: Pass-thru Query handle
            "data_size":	2+data_size,		# Known response size: 5 (units + 4-byte real in network order)
            "path":		path,			# Instance 1-8 ==> HART Channel 0-7
            "route_path":	route_path,
        },
    ]

    reply			= {}
    while not reply or reply.status == 34:		# 0 success, 34 running, 35 dead
        time.sleep( .1 )
        with io:
            for idx,dsc,req,rpy,sts,val in io.pipeline(
                    operations=client.parse_operations( operations ), **hart_kwds ):
                log.normal( "Client %s: %s --> %r: %s", io, dsc, val, enip.enip_format( rpy ))
                reply	= rpy
        log.normal( "HART pass-thru command Status: %s", reply.status )

    return reply.get( 'query.reply_data.data', None )


def test_hart_pass_thru_poll( simulated_hart_gateway ):
    r"""To test a remote C*Logix w/ a HART card, set up a remote port forward from another host in the
    same LAN.  Here's a windows example, using putty.  This windows machine (at 100.100.102.1)
    forwards a port 44818 on fat2.kundert.ca, to the PLC at 100.100.102.10:44818:

        C:\Users\Engineer\Desktop\putty.exe -R 44818:100.100.102.10:44818 perry@fat2.kundert.ca


    Now, from another host that can see fat2.kundert.ca:

        $ python -m cpppo.server.enip.list_services --list-identity -a fat2.kundert.ca:44818
        {
            "peer": [
                "fat2.kundert.ca",
                44818
            ],
            ...
            "enip.status": 0,
            "enip.CIP.list_services.CPF.count": 1,
            "enip.CIP.list_services.CPF.item[0].communications_service.capability": 288,
            "enip.CIP.list_services.CPF.item[0].communications_service.service_name": "Communications",
        }
        {
            ...
            "enip.status": 0,
            "enip.CIP.list_identity.CPF.item[0].identity_object.sin_addr": "100.100.102.10",
            "enip.CIP.list_identity.CPF.item[0].identity_object.status_word": 96,
            "enip.CIP.list_identity.CPF.item[0].identity_object.vendor_id": 1,
            "enip.CIP.list_identity.CPF.item[0].identity_object.product_name": "1756-EN2T/D",
            "enip.CIP.list_identity.CPF.item[0].identity_object.sin_port": 44818,
            "enip.CIP.list_identity.CPF.item[0].identity_object.state": 3,
            "enip.CIP.list_identity.CPF.item[0].identity_object.version": 1,
            "enip.CIP.list_identity.CPF.item[0].identity_object.device_type": 12,
            "enip.CIP.list_identity.CPF.item[0].identity_object.sin_family": 2,
            "enip.CIP.list_identity.CPF.item[0].identity_object.serial_number": 11866067,
            "enip.CIP.list_identity.CPF.item[0].identity_object.product_code": 166,
            "enip.CIP.list_identity.CPF.item[0].identity_object.product_revision": 1802,
        }

    """
    logging.getLogger().setLevel( logging.DETAIL )
    command,address             = simulated_hart_gateway

    # For testing, we'll hit a specific device
    address			= ("fat2.kundert.ca", 44818)
    #address			= ("100.100.102.10", 44818)
    #address			= ("localhost", 44818)
    route_path			= None
    route_path			= [{'link': 2, 'port': 1}]
    try:
        assert address, "Unable to detect HART EtherNet/IP CIP Gateway IP address"
        #hio				= client.implicit( host=address[0], port=address[1] )
        hio				= client.connector( host=address[0], port=address[1] )

        # Just get the primary variable, to see if the HART device is there.
        operations		= [
            {
                "method":	"service_code",
                "code":		HART.RD_VAR_REQ,
                "data":		[],			# No payload
                "data_size":	2+36,			# Known response size: command,status,<payload>
                "path":		'@0x%X/8' % ( HART.class_id ), # Instance 1-8 ==> HART Channel 0-7
                "route_path":	route_path,
            },
        ]
        
        with hio:
            for idx,dsc,req,rpy,sts,val in hio.pipeline(
                    operations=client.parse_operations( operations ), **hart_kwds ):
                log.normal( "Client %s: %s --> %r: %s", hio, dsc, val, enip.enip_format( rpy ))


        path			= '@0x%X/8' % ( HART.class_id )
        data			= hart_pass_thru(
            hio, path=path, hart_data=[1, 0], route_path=route_path, data_size=4 )

        # The small response carries the 4-byte value, the long response additionally carries the data type
        value			= None
        if data and len( data ) >= 4:
            packer		= struct.Struct( enip.REAL_network.struct_format )
            value,		= packer.unpack_from( buffer=bytearray( data[-4:] ))
        log.normal( "Read primary variable Value: %s", value )

        # HART Command 3 gets all 4 variables
        data			= hart_pass_thru(
            hio, path=path, hart_data=[3, 0], route_path=route_path, data_size=4*4 )

        # small response carries PV, SV, TV, FV values, no data types
        value			= []
        if data and len( data ) == 4*4:
            packer		= struct.Struct( enip.REAL_network.struct_format )
            for i in range( 0, len( data ), 4 ):
                value		+= packer.unpack_from( buffer=bytearray( data[i:i+4] ))
        log.normal( "Read all variables Values: %s", value )
        
    except Exception as exc:
        log.warning( "Test terminated with exception: %s", exc )
        raise


# 0x4B to HART I/O card at 0x35D/8 (HART Channel 7)  Requires a Message Router versed in HART I/O card protocol
hart_0x4b_request	= bytes(bytearray([
    0x6f, 0x00, 0x26, 0x00, 0x04, 0x00, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00, 0x6c, 0x74, 0x00, 0x00,  # o.&...1.....lt..
    0x88, 0xf9, 0x59, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08, 0x00, 0x02, 0x00,  # ..Y.............
    0x00, 0x00, 0x00, 0x00, 0xb2, 0x00, 0x16, 0x00, 0x52, 0x02, 0x20, 0x06, 0x24, 0x01, 0x05, 0xf7,  # ........R. .$...
    0x08, 0x00, 0x4b, 0x03, 0x21, 0x00, 0x5d, 0x03, 0x24, 0x08, 0x01, 0x00, 0x01, 0x02,              # ..K.!.].$.....
]))


hart_0x4b_reply		= bytes(bytearray([
                                        0x6f, 0x00,  0x38, 0x00, 0x04, 0x00, 0x31, 0x00, 0x00, 0x00,   # .b...o. 8...1...
    0x00, 0x00, 0x6c, 0x74, 0x00, 0x00, 0x88, 0xf9,  0x59, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,   # ..lt.... Y.......
    0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00,  0x00, 0x00, 0xb2, 0x00, 0x28, 0x00, 0xcb, 0x00,   # ........ ....(...
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  0x00, 0x00, 0x40, 0xa8, 0xb7, 0x41, 0x29, 0x48,   # ........ ..@..A)H
    0x9e, 0x43, 0xc2, 0x7b, 0xde, 0x43, 0x51, 0x20,  0x70, 0x3f, 0x02, 0x04, 0x0c, 0x0d, 0xc0, 0xc0,   # .C.{.CQ  p?......
    0xc0, 0xc0, 0x00, 0x00, 0x80, 0x40,                                                                # .....@
#                     ^^^^^^^^^^^^^^^^ -- unrecognized  
]))

CIP_HART_tests			= [
            ( 
                # An empty request (usually indicates termination of session)
                b'', {}
             ), (
                hart_0x4b_request,
                 {
                     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.service": 75,
                     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.segment[0].class": 861,
                     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.segment[1].instance": 8,
                     "enip.CIP.send_data.CPF.item[1].unconnected_send.request.path.size": 3,
                 }
             ), (
                hart_0x4b_reply,
                 {
                 }
             ),
]

@pytest.mark.xfail
def test_CIP_HART( repeat=1 ):
    """HART protocol enip CIP messages
    """
    enip.lookup_reset() # Flush out any existing CIP Objects for a fresh start
    #logging.getLogger().setLevel( logging.DETAIL )
    ENIP			= enip.enip_machine( context='enip' )
    CIP				= enip.CIP()
    # We'll use a HART Message Router, to handle its expanded porfolio of commands
    MR				= HART( instance_id=1 )

    for pkt,tst in client.recycle( CIP_HART_tests, times=repeat ):
        # Parse just the CIP portion following the EtherNet/IP encapsulation header
        data			= cpppo.dotdict()
        source			= cpppo.chainable( pkt )
        with ENIP as machine:
            for i,(m,s) in enumerate( machine.run( source=source, data=data )):
                log.detail( "%s #%3d -> %10.10s; next byte %3d: %-10.10r: %r",
                          machine.name_centered(), i, s, source.sent, source.peek(), data )
        # In a real protocol implementation, an empty header (EOF with no input at all) is
        # acceptable; it indicates a session closed by the client.
        if not data:
            log.normal( "EtherNet/IP Request: Empty (session terminated): %s", enip.enip_format( data ))
            continue

        if log.getEffectiveLevel() <= logging.NORMAL:
            log.normal( "EtherNet/IP Request: %s", enip.enip_format( data ))
            
        # Parse the encapsulated .input
        with CIP as machine:
            for i,(m,s) in enumerate( machine.run( path='enip', source=cpppo.peekable( data.enip.get( 'input', b'' )), data=data )):
                log.detail( "%s #%3d -> %10.10s; next byte %3d: %-10.10r: %r",
                          machine.name_centered(), i, s, source.sent, source.peek(), data )

        if log.getEffectiveLevel() <= logging.NORMAL:
            log.normal( "EtherNet/IP CIP Request: %s", enip.enip_format( data ))

        # Assume the request in the CIP's CPF items are HART requests.
        # Now, parse the encapsulated message(s).  We'll assume it is destined for a HART Object.
        if 'enip.CIP.send_data' in data:
            for item in data.enip.CIP.send_data.CPF.item:
                if 'unconnected_send.request' in item:
                    # An Unconnected Send that contained an encapsulated request (ie. not just a Get
                    # Attribute All)
                    with MR.parser as machine:
                        if log.getEffectiveLevel() <= logging.NORMAL:
                            log.normal( "Parsing %3d bytes using %s.parser, from %s", 
                                        len( item.unconnected_send.request.input ),
                                        MR, enip.enip_format( item ))
                        # Parse the unconnected_send.request.input octets, putting parsed items into the
                        # same request context
                        for i,(m,s) in enumerate( machine.run(
                                source=cpppo.peekable( item.unconnected_send.request.input ),
                                data=item.unconnected_send.request )):
                            log.detail( "%s #%3d -> %10.10s; next byte %3d: %-10.10r: %r",
                                        machine.name_centered(), i, s, source.sent, source.peek(), data )
                    # Post-processing of some parsed items is only performed after lock released!
                    if log.getEffectiveLevel() <= logging.NORMAL:
                        log.normal( "Parsed  %3d bytes using %s.parser, into %s", 
                                    len( item.unconnected_send.request.input ),
                                    MR, enip.enip_format( data ))
        try:
            for k,v in tst.items():
                assert data[k] == v, ( "data[%r] == %r\n"
                                       "expected:   %r" % ( k, data[k], v ))
        except:
            log.warning( "%r not in data, or != %r: %s", k, v, enip.enip_format( data ))
            raise
            

        # Ensure that we can get the original EtherNet/IP CIP back
        for k in list(data.keys()):
            if k.endswith( 'input' ) and 'sender_context' not in k:
                log.detail( "del data[%r]", k )
                del data[k]
        try:
            # First reconstruct any SendRRData CPF items, containing encapsulated requests/responses
            if 'enip.CIP.send_data' in data:
                cpf		= data.enip.CIP.send_data
                for item in cpf.CPF.item:
                    if 'unconnected_send' in item:
                        item.unconnected_send.request.input	= bytearray( MR.produce( item.unconnected_send.request ))
                        log.normal("Produce HART message from: %r", item.unconnected_send.request )

            # Next, reconstruct the CIP Register, ListIdentity, ListServices, or SendRRData.  The CIP.produce must
            # be provided the EtherNet/IP header, because it contains data (such as .command)
            # relevant to interpreting the .CIP... contents.
            data.enip.input		= bytearray( enip.CIP.produce( data.enip ))
            # And finally the EtherNet/IP encapsulation itself
            data.input			= bytearray( enip.enip_encode( data.enip ))
            log.detail( "EtherNet/IP CIP Request produced payload: %r", bytes( data.input ))
            assert data.input == pkt, "original:\n" + hexdump( pkt ) + "\nproduced:\n" + hexdump( data.input )
        except:
            log.warning( "Invalid packet produced from EtherNet/IP CIP data: %s", enip.enip_format( data ))
            raise

    
# 
# python hart_test.py -- A *Logix w/ a 16-channel HART Interface Card
# 
def main( **kwds ):
    """Set up a *Logix w/ a 16-channel HART Interface card, eg. 1756-IF8H"""

    enip.config_files 	       += [ __file__.replace( '.py', '.cfg' ) ]

    HART( name="HART Channels", instance_id=0 ) # Class Object
    for i in range( 16 ):
        HART( name="HART Channel %d" % i, instance_id=i + 1 )

    # Establish Identity and TCPIP objects w/ some custom data for the test, from a config file
    return enip.main( argv=sys.argv[1:] )


if __name__ == "__main__":
    sys.exit( main() )