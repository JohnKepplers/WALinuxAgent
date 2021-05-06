import os
import re
import shutil
import subprocess
import time
from datetime import datetime

from dungeon_crawler.scenarios_utils.common_tools import get_current_agent_name, execute_with_retry

__ROOT_CRON_LOG = "/var/tmp/reboot-cron-root.log"
__NON_ROOT_CRON_LOG = "/var/tmp/reboot-cron-dcr.log"
__NON_ROOT_WIRE_XML = "/var/tmp/wire-versions-dcr.xml"
__ROOT_WIRE_XML = "/var/tmp/wire-versions-root.xml"


def get_wire_ip():
    wireserver_endpoint_file = '/var/lib/waagent/WireServerEndpoint'
    try:
        with open(wireserver_endpoint_file, 'r') as f:
            wireserver_ip = f.read()
    except Exception as e:
        print("unable to read wireserver ip: {0}".format(e))
        wireserver_ip = '168.63.129.16'
        print("In the meantime -- Using the well-known WireServer address.")

    return wireserver_ip


def get_iptables_rules():
    pipe = subprocess.Popen(["iptables", "-t", "security", "-L", "-nxv"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    stdout, stderr = pipe.communicate()
    exit_code = pipe.returncode

    return exit_code, stdout.strip(), stderr.strip()


def __read_file(log_file):
    if not os.path.exists(log_file):
        raise Exception("{0} file not found!".format(log_file))

    with open(log_file) as f:
        lines = list(map(lambda _: _.strip(), f.readlines()))

    return lines


def __move_file_with_date_suffix(file_name):
    # Copy it over to /var/log/ for future debugging
    try:
        shutil.move(src=file_name, dst=os.path.join("/var", "log",
                                                    "{0}.{1}".format(os.path.basename(file_name),
                                                                     datetime.utcnow().isoformat())))
    except:
        pass


def __read_and_get_wire_versions_file(wire_version_file):
    print("\nCheck Output of wire-versions file")
    if not os.path.exists(wire_version_file):
        print("\tFile: {0} not found".format(wire_version_file))
        return None

    lines = None
    if os.stat(wire_version_file).st_size > 0:
        print("\n{0} not empty, contents: \n".format(wire_version_file))
        with open(wire_version_file) as f:
            lines = f.readlines()
        for line in lines:
            print("\t{0}".format(line.strip()))
    else:
        print("\n\t{0} is empty".format(wire_version_file))

    return lines


def __verify_data_in_cron_logs(cron_log, verify, err_msg):
    print("\nVerify Cron logs - ")

    def op():
        cron_logs_lines = __read_file(cron_log)
        if not cron_logs_lines:
            raise Exception("Empty cron file, looks like cronjob didnt run")

        if not any(verify(line) for line in cron_logs_lines):
            raise Exception("Verification failed! (UNEXPECTED): {0}".format(err_msg))

        print("\nCron logs for {0}: \n".format(cron_log))
        for line in cron_logs_lines:
            print("\t{0}".format(line))

        print("Verification succeeded. Cron logs as expected")

    execute_with_retry(op, sleep=10, max_retry=5)


def verify_wire_ip_reachable_for_root():
    # For root logs -
    # Ensure the /var/log/wire-versions-root.xml is not-empty (generated by the cron job)
    # Ensure the exit code in the /var/log/reboot-cron-root.log file is 0
    print("\nVerifying WireIP is reachable from root user - ")

    def check_exit_code(line):
        pattern = "ExitCode:\\s(\\d+)"
        return re.match(pattern, line) is not None and int(re.match(pattern, line).groups()[0]) == 0

    __verify_data_in_cron_logs(cron_log=__ROOT_CRON_LOG, verify=check_exit_code,
                               err_msg="Exit Code should be 0 for root based cron job!")

    if __read_and_get_wire_versions_file(__ROOT_WIRE_XML) is None:
        raise Exception("Wire version file should not be empty for root!")


def verify_wire_ip_unreachable_for_non_root():
    # For non-root -
    # Ensure the /var/log/wire-versions-non-root.xml is empty (generated by the cron job)
    # Ensure the exit code in the /var/log/reboot-cron-non-root.log file is non-0
    print("\nVerifying WireIP is unreachable from non-root users - ")

    def check_exit_code(line):
        match = re.match("ExitCode:\\s(\\d+)", line)
        return match is not None and int(match.groups()[0]) != 0

    __verify_data_in_cron_logs(cron_log=__NON_ROOT_CRON_LOG, verify=check_exit_code,
                               err_msg="Exit Code should be non-0 for non-root cron job!")

    if __read_and_get_wire_versions_file(__NON_ROOT_WIRE_XML) is not None:
        raise Exception("Wire version file should be empty for non-root!")


def verify_wire_ip_in_iptables(max_retry=5):
    expected_wire_ip = get_wire_ip()
    stdout, stderr = "", ""
    expected_regexes = [
        r"DROP.*{0}\s+ctstate\sINVALID,NEW.*".format(expected_wire_ip),
        r"ACCEPT.*{0}\s+owner UID match 0.*".format(expected_wire_ip)
    ]
    retry = 0
    found = False
    while retry < max_retry and not found:
        ec, stdout, stderr = get_iptables_rules()
        if not all(re.search(regex, stdout, re.MULTILINE) is not None for regex in expected_regexes):
            # Some distros take some time for iptables to setup, sleeping a bit to give it enough time
            time.sleep(30)
            retry += 1
            continue
        found = True

    print("\nIPTABLES RULES:\n\tSTDOUT: {0}".format(stdout))
    if stderr:
        print("\tSTDERR: {0}".format(stderr))

    if not found:
        raise Exception("IPTables NOT set properly - WireIP not found in IPTables")
    else:
        print("IPTables set properly")


def verify_system_rebooted():

    # This is primarily a fail safe mechanism to ensure tests don't run if the VM didnt reboot properly
    signal_file = "/var/log/reboot_time.txt"
    if not os.path.exists(signal_file):
        print("Signal file not found, checking uptime")
        __execute_and_print_cmd(["uptime", "-s"])
        raise Exception("Signal file {0} not found! Reboot didnt work as expected!".format(signal_file))

    try:
        with open(signal_file) as sig:
            reboot_time_str = sig.read().strip()

        reboot_time = datetime.strptime(reboot_time_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        now = datetime.utcnow()
        print("\nCron file Reboot time: {0}; Current Time: {1}\n".format(reboot_time_str, now.isoformat()))
        if now <= reboot_time:
            raise Exception(
                "The reboot time {0} is somehow greater than current time {1}".format(reboot_time_str, now.isoformat()))
    finally:
        # Finally delete file to keep state clean
        os.rename(signal_file, "{0}-{1}".format(signal_file, datetime.utcnow().isoformat()))


def __execute_and_print_cmd(cmd):
    pipe = subprocess.Popen(cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            shell=False)
    stdout, stderr = pipe.communicate()
    exit_code = pipe.returncode

    print(
        "\n\tCommand: {0}, ExitCode: {1}\n\tStdout: {2}\n\tStderr: {3}".format(' '.join(cmd), exit_code, stdout.strip(),
                                                                               stderr.strip()))
    return exit_code, stdout, stderr


def run_systemctl_command(service_name, command="is-enabled"):
    cmd = ["systemctl", command, service_name]
    return __execute_and_print_cmd(cmd)


def get_firewalld_rules():
    cmd = ["firewall-cmd", "--permanent", "--direct", "--get-all-passthroughs"]
    return __execute_and_print_cmd(cmd)


def get_firewalld_running_state():
    cmd = ["firewall-cmd", "--state"]
    return __execute_and_print_cmd(cmd)


def get_logs_from_journalctl(unit_name):
    cmd = ["journalctl", "-u", unit_name, "-b", "-o", "short-precise"]
    return __execute_and_print_cmd(cmd)


def generate_svg(svg_name):
    # This is a good to have, but not must have. Not failing tests if we're unable to generate a SVG
    print("Running systemd-analyze plot command to get the svg for boot execution order")
    dest_dir = os.path.join("/var", "log", "svgs")
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)
    retry = 0
    ec = 1
    while ec > 0 and retry < 3:
        cmd = "systemd-analyze plot > {0}".format(os.path.join(dest_dir, svg_name))
        print("\tCommand for Svg: {0}".format(cmd))
        pipe = subprocess.Popen(cmd,
                                shell=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        stdout, stderr = pipe.communicate()
        ec = pipe.returncode
        if stdout or stderr:
            print("\n\tSTDOUT: {0}\n\tSTDERR: {1}".format(stdout.strip(), stderr.strip()))

        if ec > 0:
            retry += 1
            print("Failed with exit-code: {0}, retrying again in 60secs. Retry Attempt: {1}".format(ec, retry))
            time.sleep(60)


def firewalld_service_enabled():
    try:
        exit_code, _, __ = get_firewalld_running_state()
        return exit_code == 0
    except Exception as error:
        print("\nFirewall service not running: {0}".format(error))

    return False


def print_stateful_debug_data():
    """
    This function is used to print all debug data that we can capture to debug the scenario (which might not be
    available on the log file). It would print the following if available (else just print the error) -
        -   The agent.service status
        -   Agent-network-setup.service status
        -   Agent-network-setup.service logs
        -   Firewall rules set using firewalld.service
        -   Output of Cron-logs for the current boot
        -   The state of iptables currently
    """
    print("\n\n\nAll possible stateful Debug data (capturing before reboot) : ")

    agent_name = get_current_agent_name()
    # -   The agent.service status
    run_systemctl_command("{0}.service".format(agent_name), "status")

    if firewalld_service_enabled():
        # -   Firewall rules set using firewalld.service
        get_firewalld_rules()

        # -   Firewalld.service status
        run_systemctl_command("firewalld.service", "status")

    else:
        # -   Agent-network-setup.service status
        run_systemctl_command("{0}-network-setup.service".format(agent_name), "status")

        # -   Agent-network-setup.service logs
        # Sometimes the service status does not return logs, calling journalctl explicitly for fetching service logs
        get_logs_from_journalctl(unit_name="{0}-network-setup.service".format(agent_name))

    # -   Print both Cron-logs contents (root and non-root) and if file is empty or not for Wire-version file
    def _print_log_data(log_file):
        try:
            log_lines = __read_file(log_file)
            if 'cron' in log_file:
                print("\nCron Logs for {0}: \n".format(log_file))
                for line in log_lines:
                    print("\t{0}".format(line))
            else:
                print("\nLog file: {0} is empty: {1}".format(log_file, not bool(log_lines)))
        except Exception as error:
            print("\nUnable to print logs for: {0}; Error: {1}".format(log_file, error))

    for test_file in [__NON_ROOT_CRON_LOG, __NON_ROOT_WIRE_XML, __ROOT_CRON_LOG, __ROOT_WIRE_XML]:
        # Move files over to the /var/log/ directory for bookkeeping
        _print_log_data(test_file)
        __move_file_with_date_suffix(test_file)

    # -   The state of iptables currently
    ec, stdout, stderr = get_iptables_rules()
    print("\nIPTABLES RULES:\n\tSTDOUT: {0}".format(stdout))
    if stderr:
        print("\tSTDERR: {0}".format(stderr))
