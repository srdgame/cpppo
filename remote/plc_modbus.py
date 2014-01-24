
# 
# Cpppo -- Communication Protocol Python Parser and Originator
# 
# Copyright (c) 2013, Hard Consulting Corporation.
# 
# Cpppo is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.  See the LICENSE file at the top of the source tree.
# 
# Cpppo is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
# 

from __future__ import absolute_import
from __future__ import print_function

__author__                      = "Perry Kundert"
__email__                       = "perry@hardconsulting.com"
__copyright__                   = "Copyright (c) 2013 Hard Consulting Corporation"
__license__                     = "Dual License: GPLv3 (or later) and Commercial (see LICENSE)"

"""
remote.plc_modbus -- Modbus/TCP PLC polling, reading and writing infrastructure
"""
__all__				= ['ModbusTcpClientTimeout', 'poller_modbus']

import logging
import select
import socket
import threading
import time
import traceback

try:
    import reprlib
except ImportError:
    import repr as reprlib


import	cpppo
from	cpppo		 import misc
from	cpppo.remote.plc import (poller, PlcOffline)

from pymodbus.constants import Defaults
from pymodbus.client.sync import ModbusTcpClient
from pymodbus.exceptions import *
from pymodbus.bit_read_message import *
from pymodbus.bit_write_message import *
from pymodbus.register_read_message import *
from pymodbus.register_write_message import *
from pymodbus.pdu import (ExceptionResponse, ModbusResponse)

if __name__ == "__main__":
    logging.basicConfig( **cpppo.log_cfg )

log				= logging.getLogger( "remote.plc_modbus" )


class ModbusTcpClientTimeout( ModbusTcpClient ):
    """
    Enforces a strict timeout on a complete transaction, including connection and I/O.  The
    beginning of a transaction is indicated by assigning a timeout to the transaction property (if
    None, uses Defaults.Timeout).  At any point, the remaining time available is computed by
    accessing the transaction property.

    If transaction is never set or set to None, Defaults.Timeout is always applied to every I/O
    operation, independently (the original behaviour).
    
    Otherwise, the specified non-zero timeout is applied to the transaction; if set to 0 or simply
    True, Defaults.Timeout is used.
    """
    def __init__(self, **kwargs):
        super( ModbusTcpClientTimeout, self ).__init__( **kwargs )
        self._started	= None
        self._timeout	= None

    @property
    def timeout( self ):
        """Returns the Defaults.Timeout, if no timeout = True|#.# (a hard timeout) has been specified."""
        if self._timeout in (None, True):
            log.debug( "Transaction timeout default: %.3fs" % ( Defaults.Timeout ))
            return Defaults.Timeout
        now		= misc.timer()
        eta		= self._started + self._timeout
        if eta > now:
            log.debug( "Transaction timeout remaining: %.3fs" % ( eta - now ))
            return eta - now
        log.debug( "Transaction timeout expired" )
        return 0
    @timeout.setter
    def timeout( self, timeout ):
        """When a self.timeout = True|0|#.# is specified, initiate a hard timeout around the following
        transaction(s).  This means that any connect and/or read/write (_recv) must complete within
        the specified timeout (Defaults.Timeout, if 'True' or 0), starting *now*.  Reset to default
        behaviour with self.timeout = None.

        """
        if timeout is None:
            self._started = None
            self._timeout = None
        else:
            self._started = misc.timer()
            self._timeout = ( Defaults.Timeout 
                              if ( timeout is True or timeout == 0 )
                              else timeout )

    def connect(self):
        """Duplicate the functionality of connect (handling optional .source_address attribute added
        in pymodbus 1.2.0), but pass the computed remaining timeout.

        """
        if self.socket: return True
        log.debug( "Connecting to (%s, %s)" % ( self.host, self.port ))
        begun			= misc.timer()
        timeout			= self.timeout # This computes the remaining timeout available
        try:
            self.socket		= socket.create_connection( (self.host, self.port),
                                    timeout=timeout, source_address=getattr( self, 'source_address', None ))
        except socket.error, msg:
            log.debug('Connection to (%s, %s) failed: %s' % \
                (self.host, self.port, msg))
            self.close()
        finally:
            log.debug( "Connect completed in %.3fs" % ( misc.timer() - begun ))

        return self.socket != None

    def _recv( self, size ):
        """On a receive timeout, closes the socket and raises a ConnectionException.  Otherwise,
        returns the available input"""
        if not self.socket:
            raise ConnectionException( self.__str_() )
        begun			= misc.timer()
        timeout			= self.timeout # This computes the remaining timeout available

        r, w, e			= select.select( [self.socket], [], [], timeout )
        if r:
            result		= super( ModbusTcpClientTimeout, self )._recv( size )
            log.debug( "Receive success in %7.3f/%7.3fs" % ( misc.timer() - begun, timeout ) )
            return result

        self.close()
        log.debug( "Receive failure in %7.3f/%7.3fs" % ( misc.timer() - begun, timeout ) )
        raise ConnectionException("Receive from (%s, %s) failed: Timeout" % (
                self.host, self.port ))

            
def shatter( address, count, limit=None ):
    """ Yields (address, count) ranges of length 'limit' sufficient to cover the
    given range.  If no limit, we'll deduce some appropriate limits for the
    deduced register type, appropriate for either multi-register reads or
    writes. """
    if not limit:
        if (        1 <= address <= 9999 
            or  10001 <= address <= 19999
            or 100001 <= address <= 165536 ):
            # Coil read/write or Status read.  
            limit	= 1968
        else:
            # Other type of register read/write (eg. Input, Holding)
            limit	= 123

    while count:
        taken		= min( count, limit or count )
        yield (address,taken)
        address	       += taken
        count	       -= taken


def merge( ranges, reach=1, limit=None ):
    """ Yields a series of independent register ranges: [(address, count), ...]
    from the provided ranges, merging any within 'reach' of each-other, with
    maximum range length 'limit'.  Will not attempt to merge addresses across a
    10000 boundary (to avoid merging different register types). """
    input		= iter( sorted( ranges ))

    base, length	= next( input )
    for address, count in input:
        if length:
            if ( address / 10000 == base / 10000
                 and address < base + length + ( reach or 1 )):
                #log.debug( "Merging:  %10r + %10r == %r" % (
                #        (base,length), (address,count), (base,address+count-base)))
                length	= address + count - base
                continue
            log.debug( "Unmerged: %10r + %10r w/reach %r" % (
                    (base,length), (address,count), reach))
            # We've been building a (base, length) merge range, but this
            # (address, count) doesn't merge; yield what we have
            for r in shatter( base, length, limit=limit ):
                log.debug( "Emitting: %10r==>%10r" % ((base,length), r ))
                yield r
        # ... and, continue from this new range
        base, length	= address, count
    # Finally, clean up whatever range we were building (if any)
    for r in shatter( base, length, limit=limit ):
        log.debug( "Emitting: %10r==>%10r w/limit %r" % ((base,length), r, limit))
        yield r


class poller_modbus( poller, threading.Thread ):
    """
    A PLC object that communicates with a physical PLC via Modbus/TCP protocol.  Schedules polls of
    various registers at various poll rates, prioritizing the polls by age.

    Writes are transmitted at the earliest opportunity, and are synchronous (ie. do not return 'til
    the write is complete, or the plc is already offline).
    
    The first completely failed poll (no successful PLC I/O transactions) marks
    the PLC as offline, and it stays offline 'til a poll again succeeds.

    Only a single PLC I/O transaction is allowed to execute on ModbusTcpClient*, with self.lock.
    """
    def __init__( self, description,
                  host='localhost', port=Defaults.Port, rate=5.0, reach=100, **kwargs ):
        poller.__init__( self, description, **kwargs )
        threading.Thread.__init__( self, target=self._poller )
        self.client		= ModbusTcpClientTimeout( host=host, port=port )
        self.lock		= threading.Lock()
        self.daemon		= True
        self.done		= False
        self.reach		= reach		# Merge registers this close into ranges
        self.start()

    def _poller( self, *args, **kwargs ):
        """ Asynchronously (ie. in another thread) poll all the specified
        registers, on the designated poll cycle.  Until we have something to do
        (self.rate isn't None), just wait.

        We'll log whenever we begin/cease polling any given range of registers.
        """
        log.info( "Poller starts: %r, %r " % ( args, kwargs ))
        polling			= set()	# Ranges known to be successfully polling
        failing			= set() # Ranges known to be failing
        target			= misc.timer()
        while not self.done and logging:  # Module may be gone in shutting down
            if self.rate is None:
                time.sleep( .1 )
                continue

            # Delay 'til poll target
            now			= misc.timer()
            if now < target:
                time.sleep( target - now )
                now		= misc.timer()

            # Ready for another poll.  Check if we've slipped (missed cycle(s))
            slipped		= int( ( now - target ) / self.rate )
            if slipped:
                log.warning( "Polling slipped; missed %d cycles" % ( slipped ))
            target	       += self.rate * ( slipped + 1 )

            # Perform polls, re-acquiring lock between each poll to allow others
            # to interject.  We'll sort the known register addresses in _data,
            # merge ranges, read the values from the PLC, and store them in
            # _data.

            # TODO: Split on and optimize counts for differing multi-register
            # limits for Coils, Registers

            # WARN: list comprehension over self._data must be atomic, because
            # we don't lock, and someone could call read/poll, adding entries to
            # self._data between reads.  However, since merge's register ranges
            # are sorted, all self._data keys are consumed before the list is
            # iterated.
            succ		= set()
            fail		= set()
            for address, count in merge( [ (a,1) for a in self._data ],
                                         reach=self.reach ):
                with self.lock:
                    try:
                        # Read values; on success (no exception, something other
                        # than None returned), immediately take online;
                        # otherwise attempts to _store will be rejected.
                        value	= self._read( address, count )
                        if not self.online:
                            self.online = True
                            log.critical( "Polling: PLC %s online; success polling %s: %s" % (
                                    self.description, address, reprlib.repr( value )))
                        if (address,count) not in polling:
                            log.warning( "Polling %6d-%-6d (%5d)" % ( address, address+count-1, count ))
                        succ.add( (address, count) )
                        self._store( address, value ) # Handle scalar or list/tuple value(s)
                    except ModbusException as exc:
                        # Modbus error; Couldn't read the given range.  Only log
                        # the first time failure to poll this range is detected
                        fail.add( (address, count) )
                        if (address, count) not in failing:
                            log.warning( "Failing %6d-%-6d (%5d): %s" % (
                                    address, address+count-1, count, str( exc )))
                    except Exception as exc:
                        # Something else; always log
                        fail.add( (address, count) )
                        log.warning( "Failing %6d-%-6d (%5d): %s" % (
                                address, address+count-1, count, traceback.format_exc() ))

            # We've already warned about polls that have failed; also log all
            # polls that have ceased (failed, or been replaced by larger polls)
            ceasing		= polling - succ - fail
            for address, count in ceasing:
                log.info( "Ceasing %6d-%-6d (%5d)" % ( address, address+count-1, count ))

            polling		= succ
            failing		= fail
            # Finally, if we've got stuff to poll and we aren't polling anything
            # successfully, and we're not yet offline, warn and take offline.
            if self._data and not polling and self.online:
                log.critical( "Polling: PLC %s offline" % ( self.description ))
                self.online	= False


    def write( self, address, value, **kwargs ):
        with self.lock:
            super( poller_modbus, self ).write( address, value, **kwargs )

    def _write( self, address, value, **kwargs ):
        """Perform the write, enforcing Defaults.Timeout around the entire transaction.
        Normally returns None, but may raise a ModbusException or a PlcOffline
        if there are communications problems.

        """
        self.client.timeout 	= True

        if not self.client.connect():
            raise PlcOffline( "Modbus/TCP Write to PLC %s/%6d failed: Offline; Connect failure" % (
                    self.description, address ))

        # Use address to deduce Holding Register or Coil (the only writable
        # entities); Statuses and Input Registers result in a pymodbus
        # ParameterException
        multi			= isinstance( value, (list,tuple) )
        writer			= None
        if 400001 <= address <= 465536:
            # 400001-465536: Holding Registers
            writer		= ( WriteMultipleRegistersRequest 
                                    if multi else WriteSingleRegisterRequest )
            address    	       -= 400001
        elif 40001 <= address <= 99999:
            # 40001-99999: Holding Registers
            writer		= ( WriteMultipleRegistersRequest if multi 
                                    else WriteSingleRegisterRequest )
            address    	       -= 40001
        elif 1 <= address <= 9999:
            # 0001-9999: Coils
            writer		= ( WriteMultipleCoilsRequest 
                                    if multi else WriteSingleCoilRequest )
            address	       -= 1
        else:
            # 100001-165536: Statuses (not writable)
            # 300001-365536: Input Registers (not writable)
            # 10001-19999: Statuses (not writable)
            # 30001-39999: Input Registers (not writable)
            pass
        if not writer:
            raise ParameterException( "Invalid Modbus address for write: %d" % ( address ))

        result			= self.client.execute( writer( address, value, **kwargs ))
        if isinstance( result, ExceptionResponse ):
            raise ModbusException( str( result ))
        assert isinstance( result, ModbusResponse ), "Unexpected non-ModbusResponse: %r" % result

    def _read( self, address, count=1, **kwargs ):
        """Perform the read, enforcing Defaults.Timeout around the entire transaction.
        Returns the result bit(s)/regsiter(s), or raises an Exception; probably
        a ModbusException or a PlcOffline for communications errors, but could
        be some other type of Exception.

        """
        self.client.timeout 	= True

        if not self.client.connect():
            raise PlcOffline( "Modbus/TCP Read  of PLC %s/%6d failed: Offline; Connect failure" % (
                    self.description, address ))
        
        # Use address to deduce Holding/Input Register or Coil/Status.
        reader			= None
        if 400001 <= address <= 465536:
            reader		= ReadHoldingRegisterRequest
            address    	       -= 400001
        elif 300001 <= address <= 365536:
            reader		= ReadInputRegisterRequest
            address    	       -= 300001
        elif 100001 <= address <= 165536:
            reader		= ReadDiscreteInputsRequest
            address    	       -= 100001
        elif 40001 <= address <= 99999:
            reader		= ReadHoldingRegistersRequest
            address    	       -= 40001
        elif 30001 <= address <= 39999:
            reader		= ReadInputRegisterRequest
            address    	       -= 30001
        elif 10001 <= address <= 19999:
            reader		= ReadDiscreteInputsRequest
            address    	       -= 10001
        elif 1 <= address <= 9999:
            reader		= ReadCoilsRequest
            address	       -= 1
        else:
            # Invalid address
            pass
        if not reader:
            raise ParameterException( "Invalid Modbus address for read: %d" % ( address ))

        result 			= self.client.execute( reader( address, count, **kwargs ))
        if isinstance( result, ExceptionResponse ):
            # The remote PLC returned a response indicating it encountered an
            # error processing the request.  Convert it to raise a ModbusException.
            raise ModbusException( str( result ))
        assert isinstance( result, ModbusResponse ), "Unexpected non-ModbusResponse: %r" % result

        # The result may contain .bits or .registers,  1 or more values
        values			= result.bits if hasattr( result, 'bits' ) else result.registers
        return values if len( values ) > 1 else values[0]

