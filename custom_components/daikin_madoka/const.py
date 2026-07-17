"""Daikin Madoka consts."""

DOMAIN = "daikin_madoka"
CONF_MAC = "address"
CONF_FRIENDLY_NAME = "friendly_name"

# Appliance driven by the BRC1H. A "thermostat" is a regular heat/cool indoor
# unit; a "ventilation" unit is a VAM (Ventilation Air Management / HRV) which
# only ventilates (operation mode 5 = VENTILATION, no heating/cooling setpoint).
CONF_DEVICE_TYPE = "device_type"
DEVICE_TYPE_THERMOSTAT = "thermostat"
DEVICE_TYPE_VENTILATION = "ventilation"
DEFAULT_DEVICE_TYPE = DEVICE_TYPE_THERMOSTAT

COORDINATORS = "coordinators"

BRC1H_NAME_PREFIX = "BRC1H"

# Advertised by the BRC1H (local_name is just "Daikin", so the service UUID is
# the reliable discovery signal). Must stay lowercase for HA matchers.
MADOKA_SERVICE_UUID = "2141e110-213a-11e6-b67b-9e71128cae77"

MIN_TEMP = 16
MAX_TEMP = 32

DEFAULT_SCAN_INTERVAL = 60
# Must exceed the connect path's internal budget (establish_connection retries
# + pairing + settle), or reconnects get cancelled mid-handshake.
CONNECT_TIMEOUT = 30
# Hard ceiling on one full-feature poll (queries retry individually).
POLL_TIMEOUT = 45
