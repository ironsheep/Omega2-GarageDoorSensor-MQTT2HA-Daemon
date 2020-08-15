#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import _thread
from datetime import datetime
from tzlocal import get_localzone
import threading
import socket
import os
import subprocess
import uuid
import ssl
import sys
import re
import json
import os.path
import argparse
from time import time, sleep, localtime, strftime
from collections import OrderedDict
from colorama import init as colorama_init
from colorama import Fore, Back, Style
from configparser import ConfigParser
from unidecode import unidecode
import paho.mqtt.client as mqtt
from signal import signal, SIGPIPE, SIG_DFL
# and for our Omega2+ hardware GPIO on Expansion board
# REF https://docs.onion.io/omega2-docs/gpio-python-module.html
import onionGpio

signal(SIGPIPE,SIG_DFL)

script_version = "1.0.0"
script_name = 'ISP-GarageSensors-mqtt-daemon.py'
script_info = '{} v{}'.format(script_name, script_version)
project_name = 'Omega2 GarageDoorSensor MQTT2HA Daemon'
project_url = 'https://github.com/ironsheep/Omega2-GarageDoorSensor-MQTT2HA-Daemon'

# we'll use this throughout
local_tz = get_localzone()

# TODO:
#  - add announcement of free-space and temperatore endpoints

if False:
    # will be caught by python 2.7 to be illegal syntax
    print_line('Sorry, this script requires a python3 runtime environment.', file=sys.stderr)

# Argparse
opt_debug = False
opt_verbose = False

# Logging function
def print_line(text, error=False, warning=False, info=False, verbose=False, debug=False, console=True, log=False):
    timestamp = strftime('%Y-%m-%d %H:%M:%S', localtime())
    if console:
        if error:
            print(Fore.RED + Style.BRIGHT + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL, file=sys.stderr)
        elif warning:
            print(Fore.YELLOW + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL)
        elif info or verbose:
            if opt_verbose:
                print(Fore.GREEN + '[{}] '.format(timestamp) + Fore.YELLOW  + '- ' + '{}'.format(text) + Style.RESET_ALL)
            else:
                print(Fore.GREEN + '[{}] '.format(timestamp) + Fore.WHITE  + '- ' + '{}'.format(text) + Style.RESET_ALL)
        elif log:
            if opt_debug:
                print(Fore.MAGENTA + '[{}] '.format(timestamp) + '- (DBG): ' + '{}'.format(text) + Style.RESET_ALL)
        elif debug:
            if opt_debug:
                print(Fore.CYAN + '[{}] '.format(timestamp) + '- (DBG): ' + '{}'.format(text) + Style.RESET_ALL)

        else:
            print(Fore.GREEN + '[{}] '.format(timestamp) + Style.RESET_ALL + '{}'.format(text) + Style.RESET_ALL)

# Identifier cleanup
def clean_identifier(name):
    clean = name.strip()
    for this, that in [[' ', '-'], ['ä', 'ae'], ['Ä', 'Ae'], ['ö', 'oe'], ['Ö', 'Oe'], ['ü', 'ue'], ['Ü', 'Ue'], ['ß', 'ss']]:
        clean = clean.replace(this, that)
    clean = unidecode(clean)
    return clean

# Argparse
parser = argparse.ArgumentParser(description=project_name, epilog='For further details see: ' + project_url)
parser.add_argument("-v", "--verbose", help="increase output verbosity", action="store_true")
parser.add_argument("-d", "--debug", help="show debug output", action="store_true")
parser.add_argument("-s", "--stall", help="TEST: report only the first time", action="store_true")
parser.add_argument("-c", '--config_dir', help='set directory where config.ini is located', default=sys.path[0])
parse_args = parser.parse_args()

config_dir = parse_args.config_dir
opt_debug = parse_args.debug
opt_verbose = parse_args.verbose
opt_stall = parse_args.stall

print_line(script_info, info=True)
if opt_verbose:
    print_line('Verbose enabled', info=True)
if opt_debug:
    print_line('Debug enabled', debug=True)
if opt_stall:
    print_line('TEST: Stall (no-re-reporting) enabled', debug=True)

# Load configuration file
config = ConfigParser(delimiters=('=', ), inline_comment_prefixes=('#'))
config.optionxform = str
try:
    with open(os.path.join(config_dir, 'config.ini')) as config_file:
        config.read_file(config_file)
except IOError:
    print_line('No configuration file "config.ini"', error=True)
    sys.exit(1)

daemon_enabled = config['Daemon'].getboolean('enabled', True)

# default domain when hostname -f doesn't return it
#default_domain = home
default_domain = ''
fallback_domain = config['Daemon'].get('fallback_domain', default_domain).lower()

default_base_topic = 'home/nodes'
base_topic_root = config['MQTT'].get('base_topic', default_base_topic).lower()

default_sensor_name = 'garage-doors'
sensor_name = config['MQTT'].get('sensor_name', default_sensor_name).lower()

default_left_name = 'left'
door_name_left = config['Doors'].get('door_1_name', default_left_name).lower()

default_right_name = 'right'
door_name_right = config['Doors'].get('door_2_name', default_right_name).lower()


# report our RPi values every 5min
min_interval_in_minutes = 2
max_interval_in_minutes = 30
default_interval_in_minutes = 5
interval_in_minutes = config['Daemon'].getint('interval_in_minutes', default_interval_in_minutes)

# Check configuration
#
if (interval_in_minutes < min_interval_in_minutes) or (interval_in_minutes > max_interval_in_minutes):
    print_line('ERROR: Invalid "interval_in_minutes" found in configuration file: "config.ini"! Must be [{}-{}] Fix and try again... Aborting'.format(min_interval_in_minutes, max_interval_in_minutes), error=True)
    sys.exit(1)

### Ensure required values within sections of our config are present
if not config['MQTT']:
    print_line('ERROR: No MQTT settings found in configuration file "config.ini"! Fix and try again... Aborting', error=True)
    sys.exit(1)

print_line('Configuration accepted', console=False)

# -----------------------------------------------------------------------------
#  MQTT handlers
# -----------------------------------------------------------------------------

# Eclipse Paho callbacks - http://www.eclipse.org/paho/clients/python/docs/#callbacks

mqtt_client_connected = False
print_line('* init mqtt_client_connected=[{}]'.format(mqtt_client_connected), debug=True)
mqtt_client_should_attempt_reconnect = True

def on_connect(client, userdata, flags, rc):
    global mqtt_client_connected
    if rc == 0:
        print_line('* MQTT connection established', console=True)
        print_line('', console=True)  # blank line?!
        #_thread.start_new_thread(afterMQTTConnect, ())
        mqtt_client_connected = True
        print_line('on_connect() mqtt_client_connected=[{}]'.format(mqtt_client_connected), debug=True)
    else:
        print_line('! Connection error with result code {} - {}'.format(str(rc), mqtt.connack_string(rc)), error=True)
        mqtt_client_connected = False   # technically NOT useful but readying possible new shape...
        print_line('on_connect() mqtt_client_connected=[{}]'.format(mqtt_client_connected), debug=True, error=True)
        #kill main thread
        os._exit(1)

def on_publish(client, userdata, mid):
    #print_line('* Data successfully published.')
    pass

def on_log(client, userdata, level, buf):
    #print_line('* Data successfully published.')
    print_line("log: {}".format(buf), debug=True, log=True)


# -----------------------------------------------------------------------------
#  RPi variables monitored
# -----------------------------------------------------------------------------

dvc_model_raw = ''
dvc_model = ''
dvc_connections = ''
dvc_hostname = ''
dvc_fqdn = ''
dvc_linux_release = ''
dvc_linux_version = ''
dvc_uptime_raw = ''
dvc_uptime = ''
dvc_mac_raw = ''
dvc_ip_addr = ''
dvc_interfaces = []
dvc_last_update_date = datetime.min
dvc_filesystem_space_raw = ''
dvc_filesystem_space = ''
dvc_filesystem_percent = ''
dvc_system_temp = ''
dvc_mqtt_script = script_info
dvc_firmware_version = ''

# Door State Indications
door_open_val = 'open'
door_closed_val = 'closed'

dvc_door_left_state = door_closed_val    # state: closed --> opening -> open -> closing...
dvc_door_right_state = door_closed_val   # state: closed --> opening -> open -> closing...
dvc_door_prior_left_state = ''
dvc_door_prior_right_state = ''



# -----------------------------------------------------------------------------
#  monitor variable fetch routines
#
def getDeviceModel():
    global dvc_model
    global dvc_model_raw
    global dvc_connections
    out = subprocess.Popen("cat /proc/cpuinfo | grep machine",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    dvc_model_raw = stdout.decode('utf-8').lstrip().rstrip()
    # now reduce string length (just more compact, same info)
    lineParts = dvc_model_raw.split(':')
    if len(lineParts) > 1:
        dvc_model = lineParts[1]
    else:
        dvc_model = ''

    # now decode interfaces
    dvc_connections = 'w' # default

    print_line('dvc_model_raw=[{}]'.format(dvc_model_raw), debug=True)
    print_line('dvc_model=[{}]'.format(dvc_model), debug=True)
    print_line('dvc_connections=[{}]'.format(dvc_connections), debug=True)

def getFirmwareVersion():
    global dvc_firmware_version
    out = subprocess.Popen("/usr/bin/oupgrade -v | tr -d '>'",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    fw_version_raw = stdout.decode('utf-8').rstrip()
    lineParts = fw_version_raw.split(':')
    dvc_firmware_version = lineParts[1].lstrip()
    print_line('dvc_firmware_version=[{}]'.format(dvc_firmware_version), debug=True)

def getProcessorType():
    global dvc_processor_family
    out = subprocess.Popen("/bin/uname -m",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    dvc_processor_family = stdout.decode('utf-8').rstrip()
    print_line('dvc_processor_family=[{}]'.format(dvc_processor_family), debug=True)

def getHostnames():
    global dvc_hostname
    global dvc_fqdn
    #  BUG?! our Omega2 doesn't know our domain name so we append it
    out = subprocess.Popen("/bin/cat /etc/config/system | /bin/grep host | /usr/bin/awk '{ print $3 }'",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    dvc_hostname = stdout.decode('utf-8').rstrip().replace("'", '')
    print_line('dvc_hostname=[{}]'.format(dvc_hostname), debug=True)
    if len(fallback_domain) > 0:
        dvc_fqdn = '{}.{}'.format(dvc_hostname, fallback_domain)
    else:
        dvc_fqdn = dvc_hostname
    print_line('dvc_fqdn=[{}]'.format(dvc_fqdn), debug=True)

def getNetworkIFs():    # RERUN in loop
    global dvc_interfaces
    global dvc_mac_raw
    global dvc_ip_addr
    out = subprocess.Popen('/sbin/ifconfig | egrep "Link|flags|inet|ether" | egrep -v -i "lo:|loopback|inet6|\:\:1|127\.0\.0\.1"',
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    lines = stdout.decode('utf-8').split("\n")
    trimmedLines = []
    for currLine in lines:
        trimmedLine = currLine.lstrip().rstrip()
        trimmedLines.append(trimmedLine)

    #print_line('trimmedLines=[{}]'.format(trimmedLines), debug=True)
    #
    # OLDER SYSTEMS
    #  eth0      Link encap:Ethernet  HWaddr b8:27:eb:c8:81:f2
    #    inet addr:192.168.100.41  Bcast:192.168.100.255  Mask:255.255.255.0
    #  wlan0     Link encap:Ethernet  HWaddr 00:0f:60:03:e6:dd
    # NEWER SYSTEMS
    #  The following means eth0 (wired is NOT connected, and WiFi is connected)
    #  eth0: flags=4099<UP,BROADCAST,MULTICAST>  mtu 1500
    #    ether b8:27:eb:1a:f3:bc  txqueuelen 1000  (Ethernet)
    #  wlan0: flags=4163<UP,BROADCAST,RUNNING,MULTICAST>  mtu 1500
    #    inet 192.168.100.189  netmask 255.255.255.0  broadcast 192.168.100.255
    #    ether b8:27:eb:4f:a6:e9  txqueuelen 1000  (Ethernet)
    #
    tmpInterfaces = []
    haveIF = False
    imterfc = ''
    for currLine in trimmedLines:
        lineParts = currLine.split()
        #print_line('- currLine=[{}]'.format(currLine), debug=True)
        #print_line('- lineParts=[{}]'.format(lineParts), debug=True)
        if len(lineParts) > 0:
            if 'flags' in currLine:  # NEWER ONLY
                haveIF = True
                imterfc = lineParts[0].replace(':', '')
                print_line('newIF=[{}]'.format(imterfc), debug=True)
            elif 'Link' in currLine:  # OLDER ONLY
                haveIF = True
                imterfc = lineParts[0].replace(':', '')
                newTuple = (imterfc, 'mac', lineParts[4])
                if dvc_mac_raw == '':
                    dvc_mac_raw = newTuple[2]
                #print_line('newIF=[{}]'.format(imterfc), debug=True)
                tmpInterfaces.append(newTuple)
                #print_line('newTuple=[{}]'.format(newTuple), debug=True)
            elif haveIF == True:
                print_line('IF=[{}], lineParts=[{}]'.format(imterfc, lineParts), debug=True)
                if 'ether' in currLine: # NEWER ONLY
                    newTuple = (imterfc, 'mac', lineParts[1])
                    tmpInterfaces.append(newTuple)
                    if dvc_mac_raw == '':
                        dvc_mac_raw = newTuple[2]
                    #print_line('newTuple=[{}]'.format(newTuple), debug=True)
                elif 'inet' in currLine:  # OLDER & NEWER
                    newTuple = (imterfc, 'IP', lineParts[1].replace('addr:',''))
                    tmpInterfaces.append(newTuple)
                    if dvc_ip_addr == '':
                        dvc_ip_addr = newTuple[2]
                    #print_line('newTuple=[{}]'.format(newTuple), debug=True)

    dvc_interfaces = tmpInterfaces
    print_line('dvc_interfaces=[{}]'.format(dvc_interfaces), debug=True)


# get model so we can use it too in MQTT
getDeviceModel()
getFirmwareVersion()
# get our hostnames so we can setup MQTT
getHostnames()
getNetworkIFs()
getProcessorType()

# -----------------------------------------------------------------------------
#  timer and timer funcs for ALIVE MQTT Notices handling
# -----------------------------------------------------------------------------

ALIVE_TIMOUT_IN_SECONDS = 60

def publishAliveStatus():
    print_line('- SEND: yes, still alive -', debug=True)
    mqtt_client.publish(lwt_topic, payload=lwt_online_val, retain=False)

def aliveTimeoutHandler():
    print_line('- MQTT TIMER INTERRUPT -', debug=True)
    _thread.start_new_thread(publishAliveStatus, ())
    startAliveTimer()

def startAliveTimer():
    global aliveTimer
    global aliveTimerRunningStatus
    stopAliveTimer()
    aliveTimer = threading.Timer(ALIVE_TIMOUT_IN_SECONDS, aliveTimeoutHandler)
    aliveTimer.start()
    aliveTimerRunningStatus = True
    print_line('- started MQTT timer - every {} seconds'.format(ALIVE_TIMOUT_IN_SECONDS), debug=True)

def stopAliveTimer():
    global aliveTimer
    global aliveTimerRunningStatus
    aliveTimer.cancel()
    aliveTimerRunningStatus = False
    print_line('- stopped MQTT timer', debug=True)

def isAliveTimerRunning():
    global aliveTimerRunningStatus
    return aliveTimerRunningStatus

# our ALIVE TIMER
aliveTimer = threading.Timer(ALIVE_TIMOUT_IN_SECONDS, aliveTimeoutHandler)
# our BOOL tracking state of ALIVE TIMER
aliveTimerRunningStatus = False



# -----------------------------------------------------------------------------
#  MQTT setup and startup
# -----------------------------------------------------------------------------

# MQTT connection

mac_basic = dvc_mac_raw.lower().replace(":", "")
mac_left = mac_basic[:6]
mac_right = mac_basic[6:]
print_line('mac lt=[{}], rt=[{}], mac=[{}]'.format(mac_left, mac_right, mac_basic), debug=True)
uniqID = "Omega2-{}gsen{}".format(mac_left, mac_right)

# our Omega2 Reporter device
LD_DOOR_LEFT = "garage_door_lt"
LD_DOOR_RIGHT = "garage_door_rt"
LDS_PAYLOAD_NAME = "info"

base_topic = '{}/binary_sensor/{}'.format(base_topic_root, sensor_name.lower())
lwt_topic = '{}/status'.format(base_topic)
lwt_online_val = 'online'
lwt_offline_val = 'offline'

print_line('Connecting to MQTT broker ...', verbose=True)
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_publish = on_publish
mqtt_client.on_log = on_log

state_topic_left = '{}/{}/state'.format(base_topic, door_name_left)
state_topic_right = '{}/{}/state'.format(base_topic, door_name_right)

mqtt_client.will_set(lwt_topic, payload=lwt_offline_val, retain=True)

if config['MQTT'].getboolean('tls', False):
    # According to the docs, setting PROTOCOL_SSLv23 "Selects the highest protocol version
    # that both the client and server support. Despite the name, this option can select
    # “TLS” protocols as well as “SSL”" - so this seems like a resonable default
    mqtt_client.tls_set(
        ca_certs=config['MQTT'].get('tls_ca_cert', None),
        keyfile=config['MQTT'].get('tls_keyfile', None),
        certfile=config['MQTT'].get('tls_certfile', None),
        tls_version=ssl.PROTOCOL_SSLv23
    )

mqtt_username = os.environ.get("MQTT_USERNAME", config['MQTT'].get('username'))
mqtt_password = os.environ.get("MQTT_PASSWORD", config['MQTT'].get('password', None))

if mqtt_username:
    mqtt_client.username_pw_set(mqtt_username, mqtt_password)
try:
    mqtt_client.connect(os.environ.get('MQTT_HOSTNAME', config['MQTT'].get('hostname', 'localhost')),
                        port=int(os.environ.get('MQTT_PORT', config['MQTT'].get('port', '1883'))),
                        keepalive=config['MQTT'].getint('keepalive', 60))
except:
    print_line('MQTT connection error. Please check your settings in the configuration file "config.ini"', error=True)
    sys.exit(1)
else:
    mqtt_client.publish(lwt_topic, payload=lwt_online_val, retain=False)
    mqtt_client.loop_start()

    while mqtt_client_connected == False: #wait in loop
        print_line('* Wait on mqtt_client_connected=[{}]'.format(mqtt_client_connected), debug=True)
        sleep(1.0) # some slack to establish the connection

    startAliveTimer()


# -----------------------------------------------------------------------------
#  Perform our MQTT Discovery Announcement...
# -----------------------------------------------------------------------------



# Binary Sensors - Device Class: garage_door
#  https://www.home-assistant.io/integrations/binary_sensor.mqtt/

# Publish our MQTT auto discovery
#  table of key items to publish:
detectorValues = OrderedDict([
    (LD_DOOR_LEFT, dict(title="GarageDr Left", subtopic=door_name_left, sensor_type="binary_sensor", device_class='garage_door', no_title_prefix="yes", device_ident='Garage Door Sensor')),
    (LD_DOOR_RIGHT, dict(title="GarageDr Right", subtopic=door_name_right, sensor_type="binary_sensor", device_class='garage_door', no_title_prefix="yes")),
])

print_line('Announcing Omega2 Monitoring device to MQTT broker for auto-discovery ...')

activity_topic = '{}/status'.format(base_topic)    # vs. LWT
command_topic_rel = '~/set'

for [sensor, params] in detectorValues.items():
    activity_topic_rel = '~/status'     # vs. LWT
    if 'commandable' in params:
        if 'subtopic' in params:
            command_topic_rel = '~/{}/set'.format(params['subtopic'])
        else:
            command_topic_rel = '~/set'
    if 'subtopic' in params:
        state_topic_rel = '~/{}/state'.format(params['subtopic'])
    else:
        state_topic_rel = '~/state'
    if 'sensor_type' in params:
        discovery_topic = 'homeassistant/{}/{}/{}/config'.format(params['sensor_type'], sensor_name.lower(), sensor)
    else:
        discovery_topic = 'homeassistant/sensor/{}/{}/config'.format(sensor_name.lower(), sensor)
    payload = OrderedDict()
    if 'no_title_prefix' in params:
        payload['name'] = "{}".format(params['title'])
    else:
        payload['name'] = "{} {}".format(sensor_name.title(), params['title'])
    payload['uniq_id'] = "{}_{}".format(uniqID, sensor.lower())
    if 'device_class' in params:
        payload['dev_cla'] = params['device_class']
    if 'unit' in params:
        payload['unit_of_measurement'] = params['unit']
    if 'icon' in params:
        payload['ic'] = params['icon']
    payload['~'] = base_topic

    # State values
    payload['pl_on'] = door_open_val
    payload['pl_off'] = door_closed_val
    payload['stat_t'] = state_topic_rel
    # LWT Values & topic
    payload['pl_avail'] = lwt_online_val
    payload['pl_not_avail'] = lwt_offline_val
    payload['avty_t'] = activity_topic_rel
    if 'commandable' in params:
        payload['cmd_t'] = command_topic_rel
    #payload['stat_val_tpl'] = '{{ value.state }}'
    payload['val_tpl'] = '{{ value_json.state }}'
    #payload['schema'] = 'json'
    if 'device_ident' in params:

        payload['dev'] = {
            'connections' : [[ "ip", dvc_ip_addr ], [ "mac", dvc_mac_raw ]],
            'identifiers' : ["{}".format(uniqID)],
            'manufacturer' : 'Onion Corporation',
            'name' : params['device_ident'],
            'model' : '{}'.format(dvc_model),
            'sw_version': "v{}".format(dvc_firmware_version)
        }
    else:
        payload['dev'] = {
            'identifiers' : ["{}".format(uniqID)],
        }
    mqtt_client.publish(discovery_topic, json.dumps(payload), 1, retain=True)

    # remove connections as test:                  'connections' : [["mac", mac.lower()], [interface, ipaddr]],

# -----------------------------------------------------------------------------
#  timer and timer funcs for period handling
# -----------------------------------------------------------------------------

TIMER_INTERRUPT = (-1)
REPORT_INTERRUPT = (-2)
TEST_INTERRUPT = (-3)

interval_counter = 0    # this is incremented until our reporting period is reached, then restarted
sensor_poll_time_in_minutes = 0.25
counter_wrap_count = (1 / sensor_poll_time_in_minutes) * interval_in_minutes

def periodTimeoutHandler():
    global interval_counter
    print_line('- PERIOD TIMER INTERRUPT -', debug=True)
    interval_counter += 1
    channel = TIMER_INTERRUPT
    if interval_counter >= counter_wrap_count:
        interval_counter = 0
        channel = REPORT_INTERRUPT
    handle_interrupt(channel) #  channel desc
    startPeriodTimer()

def startPeriodTimer():
    global endPeriodTimer
    global periodTimeRunningStatus
    stopPeriodTimer()
    # we have forced both-door reporting every interval_in_minutes but this TIMER fires every minute
    endPeriodTimer = threading.Timer(sensor_poll_time_in_minutes * 60.0, periodTimeoutHandler)
    endPeriodTimer.start()
    periodTimeRunningStatus = True
    print_line('- started PERIOD timer - every {} seconds'.format(sensor_poll_time_in_minutes * 60.0), debug=True)

def stopPeriodTimer():
    global endPeriodTimer
    global periodTimeRunningStatus
    endPeriodTimer.cancel()
    periodTimeRunningStatus = False
    print_line('- stopped PERIOD timer', debug=True)

def isPeriodTimerRunning():
    global periodTimeRunningStatus
    return periodTimeRunningStatus



# our TIMER
endPeriodTimer = threading.Timer(interval_in_minutes * 60.0, periodTimeoutHandler)
# our BOOL tracking state of TIMER
periodTimeRunningStatus = False
reported_first_time = False

# -----------------------------------------------------------------------------
#  MQTT Transmit Helper Routines
# -----------------------------------------------------------------------------
SCRIPT_TIMESTAMP = "timestamp"
GARAGE_DOOR_1 = "door1"
GARAGE_DOOR_2 = "door2"
OMEGA_LAST_UPDATE = "updated"
OMEGA_NET_CONFIG = "network"
OMEGA_SCRIPT = "script"
SCRIPT_REPORT_INTERVAL = "report_interval"

DOOR_STATE = "state"


def send_door_status(timestamp, state_value, topic):
    doorStatusData = OrderedDict()
    doorStatusData[DOOR_STATE] = state_value
    doorStatusData[SCRIPT_TIMESTAMP] = timestamp.astimezone().replace(microsecond=0).isoformat()

    _thread.start_new_thread(publishDoorValues, (doorStatusData, topic))

def getNetworkDictionary():
    global dvc_interfaces
    # TYPICAL:
    # dvc_interfaces=[[
    #   ('eth0', 'mac', 'b8:27:eb:1a:f3:bc'),
    #   ('wlan0', 'IP', '192.168.100.189'),
    #   ('wlan0', 'mac', 'b8:27:eb:4f:a6:e9')
    # ]]
    networkData = OrderedDict()

    priorIFKey = ''
    tmpData = OrderedDict()
    for currTuple in dvc_interfaces:
        currIFKey = currTuple[0]
        if priorIFKey == '':
            priorIFKey = currIFKey
        if currIFKey != priorIFKey:
            # save off prior if exists
            if priorIFKey != '':
                networkData[priorIFKey] = tmpData
                tmpData = OrderedDict()
                priorIFKey = currIFKey
        subKey = currTuple[1]
        subValue = currTuple[2]
        tmpData[subKey] = subValue
    networkData[priorIFKey] = tmpData
    print_line('networkData:{}"'.format(networkData), debug=True)
    return networkData

def publishMonitorData(latestData, topic):
    print_line('Publishing to MQTT topic "{}, Data:{}"'.format(topic, json.dumps(latestData)))
    mqtt_client.publish('{}'.format(topic), json.dumps(latestData), 1, retain=False)
    sleep(0.5) # some slack for the publish roundtrip and callback function

def publishDoorValues(latestData, topic):
    print_line('Publishing to MQTT topic "{}, Data:{}"'.format(topic, json.dumps(latestData)))
    mqtt_client.publish('{}'.format(topic), json.dumps(latestData), 1, retain=False)
    sleep(0.5) # some slack for the publish roundtrip and callback function


# -----------------------------------------------------------------------------

# Interrupt handler
def handle_interrupt(channel):
    global reported_first_time
    global dvc_door_prior_left_state
    global dvc_door_prior_right_state
    sourceID = "<< INTR(" + str(channel) + ")"
    current_timestamp = datetime.now(local_tz)
    print_line(sourceID + " >> Time to report! (%s)" % current_timestamp.strftime('%H:%M:%S - %Y/%m/%d'), verbose=True)
    # ----------------------------------
    # have PERIOD interrupt!
    updateDoorStatus()

    full_report = False
    if channel == REPORT_INTERRUPT:
        full_report = True

    if (opt_stall == False or reported_first_time == False and opt_stall == True):
        # ok, report our new state to MQTT

        # if left is new state, report it
        if full_report == True or dvc_door_prior_left_state == '' or dvc_door_prior_left_state != dvc_door_left_state:
            _thread.start_new_thread(send_door_status, (current_timestamp, dvc_door_left_state, state_topic_left))
            dvc_door_prior_left_state = dvc_door_left_state
            reported_first_time = True

        # if right is new state, report it
        if full_report == True or dvc_door_prior_right_state == '' or dvc_door_prior_right_state != dvc_door_right_state:
            _thread.start_new_thread(send_door_status, (current_timestamp, dvc_door_right_state, state_topic_right))
            dvc_door_prior_right_state = dvc_door_right_state
            reported_first_time = True
    else:
        print_line(sourceID + " >> Time to report! (%s) but SKIPPED (TEST: stall)" % current_timestamp.strftime('%H:%M:%S - %Y/%m/%d'), verbose=True)

def afterMQTTConnect():
    print_line('* afterMQTTConnect()', verbose=True)
    #  NOTE: this is run after MQTT connects

    # start our interval timer
    startPeriodTimer()
    # do our first report
    handle_interrupt(REPORT_INTERRUPT)

# -----------------------------------------------------------------------------
# GPIO Methods
#
#   Pins Wired:  I2C port retasked as GPIO
#                 - SCL -> GPIO (4) - left door
#                 - SDA -> GPIO (5) - right door
# -----------------------------------------------------------------------------
pin_left_door = 4
pin_right_door = 5

sensorLeftDoor = ''
sensorRightDoor = ''

def modeI2C():
    #   # omega2-ctrl gpiomux get
    #   Group i2c - [i2c] gpio
    #   Group uart0 - [uart] gpio
    #   Group uart1 - [uart] gpio pwm01
    #   Group uart2 - [uart] gpio pwm23
    #   Group pwm0 - pwm [gpio]
    #   Group pwm1 - pwm [gpio]
    #   Group refclk - refclk [gpio]
    #   Group spi_s - spi_s [gpio] pwm01_uart2
    #   Group spi_cs1 - [spi_cs1] gpio refclk
    #   Group i2s - i2s [gpio] pcm
    #   Group ephy - ephy [gpio]
    #   Group wled - wled [gpio]
    #
    out = subprocess.Popen("omega2-ctrl gpiomux get 2>&1 | grep -i i2c",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    pins_setup_raw = stdout.decode('utf-8').lstrip().rstrip()
    # now reduce string length (just more compact, same info)
    lineParts = pins_setup_raw.split()
    #print_line('lineParts=[{}]'.format(lineParts), debug=True)
    configureNeededStatus = False
    if '[i2c]' in pins_setup_raw:
        configureNeededStatus = True
    print_line('configureNeededStatus=[{}]'.format(configureNeededStatus), debug=True)
    return configureNeededStatus

def setPinsModeGPIO():
    #  # omega2-ctrl gpiomux get 2>&1 | grep i2c
    #  Group i2c - [i2c] gpio
    #
    out = subprocess.Popen("omega2-ctrl gpiomux set i2c gpio",
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    stdout, _ = out.communicate()
    pins_setup_raw = stdout.decode('utf-8').lstrip().rstrip()
    # now reduce string length (just more compact, same info)
    lineParts = pins_setup_raw.split()
    print_line('lineParts=[{}]'.format(lineParts), debug=True)
    # FIXME: UNDONE add validation

def doorPin(desiredDoor):
    if desiredDoor == door_name_left:
        door_pin =  pin_left_door
    else:
        door_pin =  pin_right_door
    return door_pin

def doorSensor(desiredDoor):
    if desiredDoor == door_name_left:
        door_sensor =  sensorLeftDoor
    else:
        door_sensor =  sensorRightDoor
    return door_sensor

def setUpGPIOSubsystem():
    global sensorLeftDoor
    global sensorRightDoor
    # ensure our i2c pins are set for gpio
    needConfigUse = modeI2C()
    if needConfigUse == True:
        setPinsModeGPIO()
    # now set pin directions
    sensorLeftDoor = onionGpio.OnionGpio(doorPin(door_name_left))
    sensorLeftDoor.setInputDirection()
    sensorRightDoor = onionGpio.OnionGpio(doorPin(door_name_right))
    sensorRightDoor.setInputDirection()

def readDoorStatus(desiredDoor):
    door_sensor = doorSensor(desiredDoor)
    door_pin = doorPin(desiredDoor)
    pin_value = door_sensor.getValue().rstrip()
    door_status = '{unkown}'
    if pin_value == '0':
        door_status = door_closed_val
    elif pin_value == '1':
        door_status = door_open_val
    print_line('pin {} value=[{}], door_status=[{}]'.format(door_pin, pin_value, door_status), debug=True)
    return door_status

def updateDoorStatus():
    global dvc_door_left_state
    global dvc_door_right_state
    dvc_door_left_state = readDoorStatus(door_name_left)
    dvc_door_right_state = readDoorStatus(door_name_right)

# TESTING, early abort


setUpGPIOSubsystem()    # init sensor pins
afterMQTTConnect()      # now instead of after?

# now just hang in forever loop until script is stopped externally
try:
    while True:
        #  our INTERVAL timer does the work
        sleep(10000)

finally:
    # cleanup used pins... just because we like cleaning up after us
    stopPeriodTimer()   # don't leave our timers running!
    stopAliveTimer()