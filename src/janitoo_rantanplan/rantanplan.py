# -*- coding: utf-8 -*-
"""The Raspberry rantanplan

"""

__license__ = """
    This file is part of Janitoo.

    Janitoo is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Janitoo is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with Janitoo. If not, see <http://www.gnu.org/licenses/>.

"""
__author__ = 'Sébastien GALLET aka bibi21000'
__email__ = 'bibi21000@gmail.com'
__copyright__ = "Copyright © 2013-2014-2015-2016 Sébastien GALLET aka bibi21000"

import logging
logger = logging.getLogger(__name__)
import os, sys
import threading
import datetime

from janitoo.fsm import HierarchicalMachine as Machine
from janitoo.thread import JNTBusThread, BaseThread
from janitoo.options import get_option_autostart
from janitoo.utils import HADD
from janitoo.node import JNTNode
from janitoo.value import JNTValue
from janitoo.component import JNTComponent

from janitoo_factory.buses.fsm import JNTFsmBus

from janitoo_raspberry_dht.dht import DHTComponent
from janitoo_raspberry_gpio.gpio import GpioBus, OutputComponent, PirComponent as GPIOPir, LedComponent as GPIOLed, SonicComponent
from janitoo_raspberry_1wire.bus_1wire import OnewireBus
from janitoo_raspberry_1wire.components import DS18B20
from janitoo_hostsensor_raspberry.component import HardwareCpu

##############################################################
#Check that we are in sync with the official command classes
#Must be implemented for non-regression
from janitoo.classes import COMMAND_DESC

COMMAND_WEB_CONTROLLER = 0x1030
COMMAND_WEB_RESOURCE = 0x1031
COMMAND_DOC_RESOURCE = 0x1032

assert(COMMAND_DESC[COMMAND_WEB_CONTROLLER] == 'COMMAND_WEB_CONTROLLER')
assert(COMMAND_DESC[COMMAND_WEB_RESOURCE] == 'COMMAND_WEB_RESOURCE')
assert(COMMAND_DESC[COMMAND_DOC_RESOURCE] == 'COMMAND_DOC_RESOURCE')
##############################################################

from janitoo_rantanplan import OID

def make_ambiance(**kwargs):
    return AmbianceComponent(**kwargs)

def make_led(**kwargs):
    return LedComponent(**kwargs)

def make_pir(**kwargs):
    return PirComponent(**kwargs)

def make_proximity(**kwargs):
    return ProximityComponent(**kwargs)

def make_temperature(**kwargs):
    return TemperatureComponent(**kwargs)

def make_cpu(**kwargs):
    return CpuComponent(**kwargs)


class RantanplanBus(JNTFsmBus):
    """A bus to manage Rantanplan
    """

    states = [
       'booting',
       'sleeping',
       'reporting',
       { 'name': 'guarding',
         'children': ['barking', 'bitting'],
       },
       { 'name': 'obeying',
         'children': ['barking', 'bitting'],
       },
    ]
    """The rantanplan states :
        - sleeping : bbzzzzzz...
        - reporting : only reports events as normal values ... what a good job
        - guarding : guard the zone, musts reports events as alarm values. Show I'm up by blinking my led.
            - barking : a presence have been detected, must bark to identify this guy
            - bitting : a bad guy is here, must bite it
        - obeying : Someone, somebody, something send me an order ... just obeying :
            - barking : a presence have been detected, must bark to identify this guy
            - bitting : a bad guy is here, must bite it
    """

    transitions = [
        { 'trigger': 'boot',
            'source': 'booting',
            'dest': 'sleeping',
            'conditions': 'condition_values',
        },
        { 'trigger': 'sleep',
            'source': '*',
            'dest': 'sleeping',
        },
        { 'trigger': 'wakeup',
            'source': '*',
            'dest': 'reporting',
            'conditions': 'condition_values',
        },
        { 'trigger': 'report',
            'source': '*',
            'dest': 'reporting',
            'conditions': 'condition_values',
        },
        { 'trigger': 'guard',
            'source': '*',
            'dest': 'guarding',
            'conditions': 'condition_values',
        },
        { 'trigger': 'bark',
            'source': 'guarding',
            'dest': 'guarding_barking',
        },
        { 'trigger': 'bite',
            'source': 'guarding',
            'dest': 'guarding_bitting',
        },
        { 'trigger': 'obey',
            'source': 'guarding',
            'dest': 'obeying',
        },
        { 'trigger': 'bark',
            'source': 'obeying',
            'dest': 'obeying_barking',
        },
        { 'trigger': 'bite',
            'source': 'obeying',
            'dest': 'obeying_bitting',
        },
    ]

    def __init__(self, **kwargs):
        """
        """
        JNTFsmBus.__init__(self, **kwargs)
        self.buses = {}
        self.buses['gpiobus'] = GpioBus(masters=[self], **kwargs)
        self.buses['1wire'] = OnewireBus(masters=[self], **kwargs)
        self.check_timer = None
        uuid="{:s}_timer_delay".format(OID)
        self.values[uuid] = self.value_factory['config_integer'](options=self.options, uuid=uuid,
            node_uuid=self.uuid,
            help='The delay between 2 checks',
            label='Timer.',
            default=10,
        )
        uuid="{:s}_temperature_critical".format(OID)
        self.values[uuid] = self.value_factory['config_integer'](options=self.options, uuid=uuid,
            node_uuid=self.uuid,
            help='The critical temperature. If 2 of the 3 temperature sensors are up to this value, a security notification is sent.',
            label='Critical',
            default=50,
        )
        uuid="{:s}_proximity_critical".format(OID)
        self.values[uuid] = self.value_factory['config_integer'](options=self.options, uuid=uuid,
            node_uuid=self.uuid,
            help='The distance (in cm) under which we should raise a notification.',
            label='proximity',
            default=150,
        )
        self.presence_events = {}

        uuid="{:s}_temperature".format(OID)
        self.values[uuid] = self.value_factory['sensor_temperature'](options=self.options, uuid=uuid,
            node_uuid=self.uuid,
            help='The average temperature of rantanplan. Can be use as a good quality source for a thermostat.',
            label='Temp',
        )
        poll_value = self.values[uuid].create_poll_value(default=300)
        self.values[poll_value.uuid] = poll_value

    @property
    def polled_sensors(self):
        """The sensors we will poll
        """
        return [
            self.nodeman.find_value('temperature', 'temperature'),
            self.nodeman.find_value('ambiance', 'temperature'),
            self.nodeman.find_value('ambiance', 'humidity'),
            self.nodeman.find_value('cpu', 'temperature'),
            self.nodeman.find_value('cpu', 'voltage'),
            self.nodeman.find_value('cpu', 'frequency'),
            self.nodeman.find_value('led', 'switch'),
            self.nodeman.find_value('led', 'blink'),
            self.nodeman.find_value('pir', 'status'),
            self.nodeman.find_value('proximity', 'status'),
        ]

    def condition_values(self):
        """Return True if all sensors are available
        """
        polled_sensors = self.polled_sensors
        logger.debug("[%s] - condition_values sensors : %s", self.__class__.__name__, polled_sensors)
        return all(v is not None for v in polled_sensors)

    def on_enter_reporting(self):
        """
        """
        logger.debug("[%s] - on_enter_reporting", self.__class__.__name__)
        self.fsm_bus_acquire()
        try:
            self.nodeman.find_value('led', 'blink').data = 'heartbeat'
            self.nodeman.add_polls(self.polled_sensors, slow_start=True, overwrite=False)
        except Exception:
            logger.exception("[%s] - Error in on_enter_reporting", self.__class__.__name__)
        finally:
            self.fsm_bus_release()

    def on_enter_sleeping(self):
        """
        """
        logger.debug("[%s] - on_enter_sleeping", self.__class__.__name__)
        self.fsm_bus_acquire()
        try:
            self.nodeman.remove_polls(self.polled_sensors)
            self.nodeman.find_value('led', 'blink').data = 'off'
            self.nodeman.find_bus_value('state').poll_delay = 900
        except Exception:
            logger.exception("[%s] - Error in on_enter_sleeping", self.__class__.__name__)
        finally:
            self.fsm_bus_release()

    def on_exit_sleeping(self):
        """
        """
        logger.debug("[%s] - on_exit_sleeping", self.__class__.__name__)
        self.on_check()

    def on_enter_guarding(self):
        """
        """
        logger.debug("[%s] - on_enter_guarding", self.__class__.__name__)
        self.fsm_bus_acquire()
        try:
            self.nodeman.find_value('led', 'blink').data = 'info'
            self.nodeman.add_polls(self.polled_sensors, slow_start=True, overwrite=False)
        except Exception:
            logger.exception("[%s] - Error in on_enter_guarding", self.__class__.__name__)
        finally:
            self.fsm_bus_release()

    def stop_check(self):
        """Check that the component is 'available'

        """
        if self.check_timer is not None:
            self.check_timer.cancel()
            self.check_timer = None

    def on_check(self):
        """Make a check using a timer.

        """
        self.bus_acquire()
        try:
            self.stop_check()
            if self.check_timer is None and self.is_started:
                self.check_timer = threading.Timer(self.get_bus_value('timer_delay').data)
                self.check_timer.start()
            state = True
            #Check the temperatures
            critical_temp = self.get_bus_value('temperature_critical').data
            criticals = 0
            nums = 0
            total = 0
            mini = maxi = None
            for value in [('temperature', 'temperature'), ('ambiance', 'temperature'), ('cpu', 'temperature')]:
                data = self.nodeman.find_value(*value).data
                if data is None:
                    #We should notify a sensor problem here.
                    pass
                else:
                    nums += 1
                    total += data
                    if data > critical_temp:
                        criticals += 1
                    if maxi is None or data > maxi:
                        maxi = data
                    if min is None or data < mini:
                        min = data
            if criticals > 1:
                #We should notify a security problem : fire ?
                pass
            if maxi - mini > 10:
                #We should notify a sensor problem here
                pass
            if nums != 0:
                self.get_bus_value('temperature').data = total / nums
            #Check the proximity sensors
            critical_proxi = self.get_bus_value('proximity_critical').data

        except Exception:
            logger.exception("[%s] - Error in on_check", self.__class__.__name__)
        finally:
            self.bus_release()

    def start(self, mqttc, trigger_thread_reload_cb=None, **kwargs):
        """Start the bus
        """
        for bus in self.buses:
            self.buses[bus].start(mqttc, trigger_thread_reload_cb=None, **kwargs)
        JNTFsmBus.start(self, mqttc, trigger_thread_reload_cb, **kwargs)

    def stop(self, **kwargs):
        """Stop the bus
        """
        self.stop_check()
        for bus in self.buses:
            self.buses[bus].stop(**kwargs)
        JNTFsmBus.stop(self)

    def loop(self, stopevent):
        """Retrieve data
        Don't do long task in loop. Use a separated thread to not perturbate the nodeman

        """
        for bus in self.buses:
            self.buses[bus].loop(stopevent)

class AmbianceComponent(DHTComponent):
    """ A component for ambiance """

    def __init__(self, bus=None, addr=None, **kwargs):
        """
        """
        oid = kwargs.pop('oid', '%s.ambiance'%OID)
        name = kwargs.pop('name', "Ambiance sensor")
        DHTComponent.__init__(self, oid=oid, bus=bus, addr=addr, name=name,
                **kwargs)
        logger.debug("[%s] - __init__ node uuid:%s", self.__class__.__name__, self.uuid)

class ProximityComponent(SonicComponent):
    """ A component for a proximity sensor """

    def __init__(self, bus=None, addr=None, **kwargs):
        """
        """
        oid = kwargs.pop('oid', '%s.proximity'%OID)
        name = kwargs.pop('name', "Proximity sensor")
        SonicComponent.__init__(self, oid=oid, bus=bus, addr=addr, name=name,
                **kwargs)
        logger.debug("[%s] - __init__ node uuid:%s", self.__class__.__name__, self.uuid)

class PirComponent(GPIOPir):
    """ A component for a PIR """

    def __init__(self, bus=None, addr=None, **kwargs):
        """
        """
        oid = kwargs.pop('oid', '%s.pir'%OID)
        name = kwargs.pop('name', "Pir sensor")
        GPIOPir.__init__(self, oid=oid, bus=bus, addr=addr, name=name,
                **kwargs)
        logger.debug("[%s] - __init__ node uuid:%s", self.__class__.__name__, self.uuid)

class LedComponent(GPIOLed):
    """ A component for a Led (on/off) """

    def __init__(self, bus=None, addr=None, **kwargs):
        """
        """
        oid = kwargs.pop('oid', '%s.led'%OID)
        name = kwargs.pop('name', "Led")
        GPIOLed.__init__(self, oid=oid, bus=bus, addr=addr, name=name,
                **kwargs)
        logger.debug("[%s] - __init__ node uuid:%s", self.__class__.__name__, self.uuid)

class TemperatureComponent(DS18B20):
    """ A water temperature component """

    def __init__(self, bus=None, addr=None, **kwargs):
        """
        """
        oid = kwargs.pop('oid', '%s.temperature'%OID)
        name = kwargs.pop('name', "Temperature")
        DS18B20.__init__(self, oid=oid, bus=bus, addr=addr, name=name,
                **kwargs)
        logger.debug("[%s] - __init__ node uuid:%s", self.__class__.__name__, self.uuid)

class CpuComponent(HardwareCpu):
    """ A water temperature component """

    def __init__(self, bus=None, addr=None, **kwargs):
        """
        """
        oid = kwargs.pop('oid', '%s.cpu'%OID)
        name = kwargs.pop('name', "CPU")
        HardwareCpu.__init__(self, oid=oid, bus=bus, addr=addr, name=name,
                **kwargs)
        logger.debug("[%s] - __init__ node uuid:%s", self.__class__.__name__, self.uuid)
