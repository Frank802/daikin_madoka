import esphome.codegen as cg
from esphome.components import (
    ble_client,
    climate,
    sensor,
    text_sensor,
)
import esphome.config_validation as cv
from esphome.const import (
    CONF_ID,
    DEVICE_CLASS_TEMPERATURE,
    STATE_CLASS_MEASUREMENT,
    UNIT_CELSIUS,
)

CODEOWNERS = ["@Frank802"]
DEPENDENCIES = ["ble_client"]
AUTO_LOAD = ["sensor", "text_sensor"]

CONF_OUTDOOR_TEMPERATURE = "outdoor_temperature"
CONF_FIRMWARE_VERSION = "firmware_version"
CONF_DUMP_RAW = "dump_raw"

madoka_vam_ns = cg.esphome_ns.namespace("madoka_vam")
MadokaVam = madoka_vam_ns.class_(
    "MadokaVam", climate.Climate, ble_client.BLEClientNode, cg.PollingComponent
)

CONFIG_SCHEMA = (
    climate.climate_schema(MadokaVam)
    .extend(ble_client.BLE_CLIENT_SCHEMA)
    .extend(cv.polling_component_schema("10s"))
    .extend(
        {
            cv.Optional(CONF_OUTDOOR_TEMPERATURE): sensor.sensor_schema(
                unit_of_measurement=UNIT_CELSIUS,
                accuracy_decimals=0,
                device_class=DEVICE_CLASS_TEMPERATURE,
                state_class=STATE_CLASS_MEASUREMENT,
            ),
            cv.Optional(CONF_FIRMWARE_VERSION): text_sensor.text_sensor_schema(
                icon="mdi:chip",
            ),
            # Journalise en hexadécimal chaque trame BLE échangée : aide au
            # reverse engineering des fonctions spécifiques au VAM.
            cv.Optional(CONF_DUMP_RAW, default=False): cv.boolean,
        }
    )
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await climate.register_climate(var, config)
    await ble_client.register_ble_node(var, config)

    cg.add(var.set_dump_raw(config[CONF_DUMP_RAW]))

    if conf := config.get(CONF_OUTDOOR_TEMPERATURE):
        outdoor_sensor = await sensor.new_sensor(conf)
        cg.add(var.set_outdoor_temperature_sensor(outdoor_sensor))

    if conf := config.get(CONF_FIRMWARE_VERSION):
        firmware_sensor = await text_sensor.new_text_sensor(conf)
        cg.add(var.set_firmware_version_text_sensor(firmware_sensor))
