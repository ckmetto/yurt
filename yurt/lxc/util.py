import logging
import os
from typing import List, Dict
import pylxd

from yurt import config
from yurt import vm
from yurt.exceptions import LXCException, VMException


NETWORK_NAME = "yurt-int"
PROFILE_NAME = "yurt"
REMOTES = {
    "images": {
        "Name": "images",
        "URL": "https://images.linuxcontainers.org",
    },
    "ubuntu": {
        "Name": "ubuntu",
        "URL": "https://cloud-images.ubuntu.com/releases",
    },
}


def _setup_yurt_socat():
    name = "yurt-lxd-socat"
    vm.run_cmd("mkdir -p /tmp/yurt")
    tmp_unit_file = f"/tmp/yurt/{name}.service"
    installed_unit_file = f"/etc/systemd/system/{name}.service"
    vm.run_cmd("sudo apt install socat -y")
    vm.put_file(os.path.join(config.provision_dir,
                             f"{name}.service"), tmp_unit_file)
    vm.run_cmd(f"sudo cp {tmp_unit_file} {installed_unit_file}")
    vm.run_cmd("sudo systemctl daemon-reload")
    vm.run_cmd(f"sudo systemctl enable {name}")
    vm.run_cmd(f"sudo systemctl start {name}")


def get_pylxd_client():
    lxd_port = config.get_config(config.Key.lxd_port)
    try:
        return pylxd.Client(endpoint=f"http://127.0.0.1:{lxd_port}")
    except pylxd.exceptions.ClientConnectionFailed as e:
        logging.debug(e)
        raise LXCException(
            "Error connecting to LXD. Try restarting the VM: 'yurt vm restart'")


def get_instance(name: str):
    client = get_pylxd_client()
    try:
        return client.instances.get(name)  # pylint: disable=no-member
    except pylxd.exceptions.NotFound:
        raise LXCException(f"Instance {name} not found.")
    except pylxd.exceptions.LXDAPIException:
        raise LXCException(
            f"Could not fetch instance {name}. API Error.")


def is_initialized():
    return config.get_config(config.Key.is_lxd_initialized)


def get_ip_config():
    from ipaddress import ip_interface

    host_ip_address = config.get_config(
        config.Key.interface_ip_address)
    network_mask = config.get_config(
        config.Key.interface_netmask)
    if not (host_ip_address and network_mask):
        raise LXCException("Bad IP Configuration. ip: {0}, mask: {1}".format(
            host_ip_address, network_mask))

    full_host_address = ip_interface(
        "{0}/{1}".format(host_ip_address, network_mask))
    bridge_address = ip_interface(
        "{0}/{1}".format((full_host_address + 1).ip, network_mask)).exploded

    return {
        "bridgeAddress": bridge_address,
        "dhcpRangeLow": (full_host_address + 10).ip.exploded,
        "dhcpRangeHigh": (full_host_address + 249).ip.exploded
    }


def initialize_lxd():
    if is_initialized():
        return

    try:
        with open(os.path.join(config.provision_dir, "lxd-init.yaml"), "r") as f:
            init = f.read()
    except OSError as e:
        raise LXCException(f"Error reading lxd-init.yaml {e}")

    try:
        logging.info("Updating package information...")
        vm.run_cmd("sudo apt update", show_spinner=True)
        vm.run_cmd("sudo usermod yurt -a -G lxd")

        logging.info("Initializing LXD...")
        vm.run_cmd(
            "sudo lxd init --preseed",
            stdin=init,
            show_spinner=True
        )
        _setup_yurt_socat()

        logging.info("Done.")
        config.set_config(config.Key.is_lxd_initialized, True)
    except VMException as e:
        logging.error(e)
        logging.error("Restart the VM to try again: 'yurt vm restart'")
        raise LXCException("Failed to initialize LXD.")


def check_network_config():
    client = get_pylxd_client()
    if client.networks.exists(NETWORK_NAME):  # pylint: disable=no-member
        return

    logging.info("Configuring network...")
    ip_config = get_ip_config()
    bridge_address = ip_config["bridgeAddress"]
    dhcp_range_low = ip_config["dhcpRangeLow"]
    dhcp_range_high = ip_config["dhcpRangeHigh"]

    client.networks.create(  # pylint: disable=no-member
        NETWORK_NAME, description="Yurt Network", type="bridge",
        config={
            "bridge.external_interfaces": "enp0s8",
            "ipv6.address": "none",
            "ipv4.nat": "true",
            "ipv4.dhcp": "true",
            "ipv4.dhcp.expiry": "24h",
            "ipv4.address": bridge_address,
            "ipv4.dhcp.ranges": f"{dhcp_range_low}-{dhcp_range_high}",
            "dns.domain": config.app_name
        })


def check_profile_config():
    client = get_pylxd_client()
    if client.profiles.exists(PROFILE_NAME):  # pylint: disable=no-member
        return

    logging.info("Configuring profile...")
    client.profiles.create(  # pylint: disable=no-member
        PROFILE_NAME,
        devices={
            "eth0": {
                "name": "eth0",
                "nictype": "bridged",
                "parent": NETWORK_NAME,
                "type": "nic"
            },
            "root": {
                "type": "disk",
                "pool": "yurtpool",
                "path": "/"
            }
        }
    )


def shortest_alias(aliases: List[Dict[str, str]], remote: str):
    import re

    aliases = list(map(lambda a: str(a["name"]), aliases))
    if remote == "ubuntu":
        aliases = list(filter(lambda a: re.match(
            r"^\d\d\.\d\d", a), aliases))

    try:
        alias = aliases[0]
        for a in aliases:
            if len(a) < len(alias):
                alias = a
        return alias
    except (IndexError, KeyError) as e:
        logging.debug(e)
        logging.error(f"Unexpected alias schema: {aliases}")


def filter_remote_images(images: List[Dict]):
    aliased = filter(lambda i: i["aliases"],  images)
    container = filter(
        lambda i: i["type"] == "container", aliased)
    x64 = filter(
        lambda i: i["architecture"] == "x86_64", container)

    return x64


def get_remote_image_info(remote: str, image: Dict):
    try:
        return {
            "Alias": shortest_alias(image["aliases"], remote),
            "Description": image["properties"]["description"]
        }
    except KeyError as e:
        logging.debug(e)
        logging.debug(f"Unexpected image schema: {image}")


def exec_interactive(instance_name: str, cmd: List[str], environment=None):
    from . import term

    instance = get_instance(instance_name)
    response = instance.raw_interactive_execute(cmd, environment=environment)
    lxd_port = config.get_config(config.Key.lxd_port)
    try:

        ws_url = f"ws://127.0.0.1:{lxd_port}{response['ws']}"
        term.run(ws_url)
    except KeyError as e:
        raise LXCException(f"Missing ws URL {e}")


def unpack_download_operation_metadata(metadata):
    if metadata:
        if "download_progress" in metadata:
            return f"Download progress: {metadata['download_progress']}"
        if "create_instance_from_image_unpack_progress" in metadata:
            return f"Unpack progress: {metadata['create_instance_from_image_unpack_progress']}"
    else:
        return ""


def follow_operation(operation_uri: str, unpack_metadata=None):
    """
    Params:
        operation_uri:      URI of the operation to follow.
        unpack_metadata:    Function to unpack the operation's metadata. Return a line of text to summarize
                            the current progress of the operation.
                            If not given, progress will not be shown.
    """
    import time
    from yurt.util import retry

    operations = get_pylxd_client().operations

    # Allow time for operation to be created.
    try:
        retry(
            lambda: operations.get(operation_uri),  # pylint: disable=no-member
            retries=10,
            wait_time=0.5
        )
        operation = operations.get(operation_uri)  # pylint: disable=no-member
    except pylxd.exceptions.NotFound:
        raise LXCException(
            f"Timed out while waiting for operation to be created.")

    logging.info(operation.description)
    while True:
        try:
            operation = operations.get(  # pylint: disable=no-member
                operation_uri
            )
            if unpack_metadata:
                print(f"\r{unpack_metadata(operation.metadata)}", end="")
            time.sleep(0.5)
        except pylxd.exceptions.NotFound:
            print("\nDone")
            break
        except KeyboardInterrupt:
            break
