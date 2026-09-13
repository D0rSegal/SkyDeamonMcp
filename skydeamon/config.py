"""SkyDemon server config — values reverse-engineered from SkyDemon.exe.

Decompiler refs (tmp/decompiled/SkyDemon.decompiled.cs):
- InternetConnectivity.ServerAddress => "data.skydemon.aero"
- InternetConnectivity.GetUrlForApi(api) => "https://" + ServerAddress + "/" + api
- ApplicationEnvironment.ProductName => "SkyDemon Plan"
"""

SERVER_ADDRESS = "data.skydemon.aero"

BASE_URL = f"https://{SERVER_ADDRESS}"

PRODUCT_NAME = "SkyDemon Plan"
# Installed SkyDemon.exe assembly version (C:\Program Files (x86)\SkyDemon\SkyDemon.exe)
PRODUCT_VERSION = "4.3.2.29207"

# SkyDemonLicensing GUIDs (SkyDemon.decompiled.cs ~line 13073)
PLANNING_PRODUCT_GUID = "c1b63f41-0451-47fd-875f-359d19522e20"
PLATE_PRODUCT_GUID = "d0efbe3f-d347-4313-a983-865a0986e6f8"
LICENSES_UPDATED_GUID = "540cdbd6-ce3d-41d9-89df-4db8e5b7d48e"

# DeviceLogin binary magic (DeviceLogin.Deserialize, ~line 12900)
DEVICE_LOGIN_MAGIC = 64236213


def get_url_for_api(api: str) -> str:
    """Mirror of InternetConnectivity.GetUrlForApi()."""
    return f"{BASE_URL}/{api.lstrip('/')}"""
