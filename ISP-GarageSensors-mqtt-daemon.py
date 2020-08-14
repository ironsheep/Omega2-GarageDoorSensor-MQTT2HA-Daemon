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
# and for our Omega2+ hardware
from OmegaExpansion import relayExp

signal(SIGPIPE,SIG_DFL)

script_version = "0.0.1"
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
door_opening_val = 'opening'
door_closed_val = 'closed'
door_closing_val = 'closing'

dvc_door_left_state_indication = door_closed_val    # state: closed --> opening -> open -> closing...
dvc_door_right_state_indication = door_closed_val   # state: closed --> opening -> open -> closing...



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
    stdout,_ = out.communicate()
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

def getLinuxRelease():
    global dvc_linux_release
    dvc_linux_release = 'openWrt'
    print_line('dvc_linux_release=[{}]'.format(dvc_linux_release), debug=True)

def getLinuxVersion():
    global dvc_linux_version
    out = subprocess.Popen("/bin/uname -r", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_linux_version = stdout.decode('utf-8').rstrip()
    print_line('dvc_linux_version=[{}]'.format(dvc_linux_version), debug=True)
    
def getFirmwareVersion():
    global dvc_firmware_version
    out = subprocess.Popen("/usr/bin/oupgrade -v | tr -d '>'", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
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
    stdout,_ = out.communicate()
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
    stdout,_ = out.communicate()
    dvc_hostname = stdout.decode('utf-8').rstrip().replace("'", '')
    print_line('dvc_hostname=[{}]'.format(dvc_hostname), debug=True)
    if len(fallback_domain) > 0:
        dvc_fqdn = '{}.{}'.format(dvc_hostname, fallback_domain)
    else:
        dvc_fqdn = dvc_hostname
    print_line('dvc_fqdn=[{}]'.format(dvc_fqdn), debug=True)

def getUptime():    # RERUN in loop
    global dvc_uptime_raw
    global dvc_uptime
    out = subprocess.Popen("/usr/bin/uptime", 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_uptime_raw = stdout.decode('utf-8').rstrip().lstrip()
    print_line('dvc_uptime_raw=[{}]'.format(dvc_uptime_raw), debug=True)
    basicParts = dvc_uptime_raw.split()
    timeStamp = basicParts[0]
    lineParts = dvc_uptime_raw.split(',')
    if('user' in lineParts[1]):
        dvc_uptime_raw = lineParts[0]
    else:
        dvc_uptime_raw = '{}, {}'.format(lineParts[0], lineParts[1])
    dvc_uptime = dvc_uptime_raw.replace(timeStamp, '').lstrip().replace('up ', '')
    print_line('dvc_uptime=[{}]'.format(dvc_uptime), debug=True)

def getNetworkIFs():    # RERUN in loop
    global dvc_interfaces
    global dvc_mac_raw
    out = subprocess.Popen('/sbin/ifconfig | egrep "Link|flags|inet|ether" | egrep -v -i "lo:|loopback|inet6|\:\:1|127\.0\.0\.1"', 
           shell=True,
           stdout=subprocess.PIPE, 
           stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
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
                    dvc_mac_raw = lineParts[4]
                #print_line('newIF=[{}]'.format(imterfc), debug=True)
                tmpInterfaces.append(newTuple)
                #print_line('newTuple=[{}]'.format(newTuple), debug=True)
            elif haveIF == True:
                print_line('IF=[{}], lineParts=[{}]'.format(imterfc, lineParts), debug=True)
                if 'ether' in currLine: # NEWER ONLY
                    newTuple = (imterfc, 'mac', lineParts[1])
                    tmpInterfaces.append(newTuple)
                    #print_line('newTuple=[{}]'.format(newTuple), debug=True)
                elif 'inet' in currLine:  # OLDER & NEWER
                    newTuple = (imterfc, 'IP', lineParts[1].replace('addr:',''))
                    tmpInterfaces.append(newTuple)
                    #print_line('newTuple=[{}]'.format(newTuple), debug=True)

    dvc_interfaces = tmpInterfaces
    print_line('dvc_interfaces=[{}]'.format(dvc_interfaces), debug=True)

def getFileSystemSpace():    # RERUN in loop
    global dvc_filesystem_space_raw
    global dvc_filesystem_space
    global dvc_filesystem_percent
    out = subprocess.Popen("/bin/df -m | /bin/grep root", 
            shell=True,
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT)
    stdout,_ = out.communicate()
    dvc_filesystem_space_raw = stdout.decode('utf-8').rstrip()
    print_line('dvc_filesystem_space_raw=[{}]'.format(dvc_filesystem_space_raw), debug=True)
    lineParts = dvc_filesystem_space_raw.split()
    print_line('lineParts=[{}]'.format(lineParts), debug=True)
    filesystem_1GBlocks = int(lineParts[1],10) / 1024
    if filesystem_1GBlocks > 32:
        dvc_filesystem_space = '64GB'
    elif filesystem_1GBlocks > 16:
        dvc_filesystem_space = '32GB'
    elif filesystem_1GBlocks > 8:
        dvc_filesystem_space = '16GB'
    elif filesystem_1GBlocks > 4:
        dvc_filesystem_space = '8GB'
    elif filesystem_1GBlocks > 2:
        dvc_filesystem_space = '4GB'
    elif filesystem_1GBlocks > 1:
        dvc_filesystem_space = '2GB'
    else:
        dvc_filesystem_space = '1GB'
    print_line('dvc_filesystem_space=[{}]'.format(dvc_filesystem_space), debug=True)
    dvc_filesystem_percent = lineParts[4].replace('%', '')
    print_line('dvc_filesystem_percent=[{}]'.format(dvc_filesystem_percent), debug=True)

def getLastUpdateDate():    # RERUN in loop
    global dvc_last_update_date
    apt_log_filespec = '/var/opkg-lists/omega2_base.sig'
    try:
        mtime = os.path.getmtime(apt_log_filespec)
    except OSError:
        mtime = 0
    last_modified_date = datetime.fromtimestamp(mtime, tz=local_tz)
    dvc_last_update_date  = last_modified_date
    print_line('dvc_last_update_date=[{}]'.format(dvc_last_update_date), debug=True)

# get model so we can use it too in MQTT
getDeviceModel()
getFirmwareVersion()
# get our hostnames so we can setup MQTT
getHostnames()
getLastUpdateDate()
getLinuxRelease()
getLinuxVersion()
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

base_topic = '{}/cover/{}'.format(base_topic_root, sensor_name.lower())
lwt_topic = '{}/status'.format(base_topic)
lwt_online_val = 'online'
lwt_offline_val = 'offline'

print_line('Connecting to MQTT broker ...', verbose=True)
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_publish = on_publish
mqtt_client.on_log = on_log

command_topic_left = '{}/{}/set'.format(base_topic, door_name_left)

command_topic_right = '{}/{}/set'.format(base_topic, door_name_right)

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

mac_basic = dvc_mac_raw.lower().replace(":", "")
mac_left = mac_basic[:6]
mac_right = mac_basic[6:]
print_line('mac lt=[{}], rt=[{}], mac=[{}]'.format(mac_left, mac_right, mac_basic), debug=True)
uniqID = "Omega2-{}gar{}".format(mac_left, mac_right)

# our Omega2 Reporter device
LD_DOOR_LEFT = "garage_door_lt"
LD_DOOR_RIGHT = "garage_door_rt"
LDS_PAYLOAD_NAME = "info"

# FULL CONFIGURATION STATE TOPIC WITHOUT TILT
#  https://www.home-assistant.io/integrations/cover.mqtt/#full-configuration-state-topic-without-tilt

# Publish our MQTT auto discovery
#  table of key items to publish:
detectorValues = OrderedDict([
    (LD_DOOR_LEFT, dict(title="GarageDr Left", subtopic=door_name_left, sensor_type="binary_sensor", device_class='garage_door', icon='mdi:garage', no_title_prefix="yes", device_ident='Garage Door Sensor')),
    (LD_DOOR_RIGHT, dict(title="GarageDr Right", subtopic=door_name_right, sensor_type="binary_sensor", device_class='garage_door', icon='mdi:garage', no_title_prefix="yes")),
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
    payload['stat_clsd'] = door_closed_val
    payload['stat_open'] = door_open_val
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
TEST_INTERRUPT = (-2)

def periodTimeoutHandler():
    print_line('- PERIOD TIMER INTERRUPT -', debug=True)
    handle_interrupt(TIMER_INTERRUPT) # '0' means we have a timer interrupt!!!
    startPeriodTimer()

def startPeriodTimer():
    global endPeriodTimer
    global periodTimeRunningStatus
    stopPeriodTimer()
    endPeriodTimer = threading.Timer(interval_in_minutes * 60.0, periodTimeoutHandler) 
    endPeriodTimer.start()
    periodTimeRunningStatus = True
    print_line('- started PERIOD timer - every {} seconds'.format(interval_in_minutes * 60.0), debug=True)

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

def send_status(timestamp, nothing):

    omegaData = OrderedDict()
    omegaData[SCRIPT_TIMESTAMP] = timestamp.astimezone().replace(microsecond=0).isoformat()
    omegaData[GARAGE_DOOR_1] = dvc_model
    omegaData[GARAGE_DOOR_1] = dvc_connections

    if dvc_last_update_date != datetime.min:
        omegaData[OMEGA_LAST_UPDATE] = dvc_last_update_date.astimezone().replace(microsecond=0).isoformat()
    else:
        omegaData[OMEGA_LAST_UPDATE] = ''

    omegaData[OMEGA_NET_CONFIG] = getNetworkDictionary()

    omegaData[OMEGA_SCRIPT] = dvc_mqtt_script.replace('.py', '')
    omegaData[SCRIPT_REPORT_INTERVAL] = interval_in_minutes

    omegaTopDict = OrderedDict()
    omegaTopDict[LDS_PAYLOAD_NAME] = omegaData

    _thread.start_new_thread(publishMonitorData, (omegaTopDict, state_topic_right))

def send_door_status(timestamp, topic):
    global dvc_door_right_state_indication
    global dvc_door_left_state_indication
    omegaData = OrderedDict()

    if door_name_left in topic:
        state_value =  dvc_door_left_state_indication
    else:
        state_value =  dvc_door_right_state_indication

    omegaData[DOOR_STATE] = state_value
    omegaData[SCRIPT_TIMESTAMP] = timestamp.astimezone().replace(microsecond=0).isoformat()

    _thread.start_new_thread(publishDoorValues, (omegaData, topic))

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

def update_values():
    # nothing here yet
    getUptime()
    #getFileSystemSpace()
    getLastUpdateDate()
    getNetworkIFs()

    

# -----------------------------------------------------------------------------

# Interrupt handler
def handle_interrupt(channel):
    global reported_first_time
    sourceID = "<< INTR(" + str(channel) + ")"
    current_timestamp = datetime.now(local_tz)
    print_line(sourceID + " >> Time to report! (%s)" % current_timestamp.strftime('%H:%M:%S - %Y/%m/%d'), verbose=True)
    # ----------------------------------
    # have PERIOD interrupt!
    update_values()

    if (opt_stall == False or reported_first_time == False and opt_stall == True):
        # ok, report our new detection to MQTT
        _thread.start_new_thread(send_door_status, (current_timestamp, state_topic_left))
        _thread.start_new_thread(send_door_status, (current_timestamp, state_topic_right))
        reported_first_time = True
    else:
        print_line(sourceID + " >> Time to report! (%s) but SKIPPED (TEST: stall)" % current_timestamp.strftime('%H:%M:%S - %Y/%m/%d'), verbose=True)
    
def afterMQTTConnect():
    print_line('* afterMQTTConnect()', verbose=True)
    #  NOTE: this is run after MQTT connects

    print_line('* SUBSCRIBE to [{}]'.format(command_topic_left), verbose=True)
    mqtt_client.subscribe(command_topic_left)
    print_line('* SUBSCRIBE to [{}]'.format(command_topic_right), verbose=True)
    mqtt_client.subscribe(command_topic_right)

    # start our interval timer
    startPeriodTimer()
    # do our first report
    handle_interrupt(0)

# TESTING AGAIN


# TESTING, early abort

afterMQTTConnect()  # now instead of after?

# now just hang in forever loop until script is stopped externally
try:
    while True:
        #  our INTERVAL timer does the work
        sleep(10000)
        
finally:
    # cleanup used pins... just because we like cleaning up after us
    stopPeriodTimer()   # don't leave our timers running!
    stopAliveTimer()
