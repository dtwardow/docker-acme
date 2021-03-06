#!/usr/bin/env python
import logging
import os
import re
import shutil
import subprocess
import time
import configparser
from datetime import datetime
from urllib.request import urlopen

import acme_tiny

crt_dir = "crt/"
crt_bak_dir = "crt/backup/"
tmp_dir = "/tmp/"
acme_challenge_dir = "acme_challenge/"

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

STAGING_CA = "https://acme-staging.api.letsencrypt.org"
DEFAULT_CA = "https://acme-v01.api.letsencrypt.org"

crt_max_age = os.getenv("CRT_MAX_AGE", 30)  # in days
chained_crt = os.getenv("CHAINED_CRT", "true")
acme_ca = os.getenv("ACME_CA", DEFAULT_CA)
acme_intermediate = os.getenv("ACME_INTERMEDIATE", "https://letsencrypt.org/certs/lets-encrypt-x3-cross-signed.pem")
container_notify = os.getenv("CONTAINER_NOTIFY")
dh_max_age = os.getenv("DH_MAX_AGE")


def check_crt(name, domains):
    crt_file = "%s/%s.crt" % (crt_dir, name)
    csr_file = "%s/%s.csr" % (crt_dir, name)

    if not os.path.isfile(crt_file) or not os.path.isfile(csr_file):
        return False

    # CSR info
    proc = subprocess.Popen(
        ["openssl", "req", "-noout", "-text",
         "-in", csr_file],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)

    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))

    # get domain list from CSR info
    match = re.search(r"X509v3 Subject Alternative Name:(?: critical)?\s*(.*)",
                      out.decode("utf8"))
    sans_parts = [] if match is None else match.group(1).split(", ")
    csr_domains = sorted([part.split(":")[1] for part in sans_parts if part.startswith("DNS:")])

    if csr_domains != domains:
        return False

    # check certificate start time
    proc = subprocess.Popen(
        ["openssl", "x509", "-noout", "-startdate",
         "-in", crt_file],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)

    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))

    start_time = datetime.strptime(out.decode("utf8").strip().split("=")[1],
                                   "%b %d %H:%M:%S %Y %Z")

    return (datetime.now()-start_time).days < int(crt_max_age)


def exist_key(name):
    return os.path.isfile("%s/%s.key" % (crt_dir, name))


def create_key(name):
    if exist_key(name):
        shutil.copyfile(
            "%s/%s.key" % (crt_dir, name),
            "%s/%s_%s.key" % (crt_bak_dir,
                              datetime.now().strftime("%Y%m%d_%H%M%S"),
                              name))

    proc = subprocess.Popen(
        ["openssl", "genrsa", "4096"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)

    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))

    with open("%s/%s.key" % (crt_dir, name), "wb") as file:
        file.write(out)


def create_csr(name, domains):
    ssl_conf = tmp_dir + "openssl.cnf"
    shutil.copyfile("/etc/ssl/openssl.cnf", ssl_conf)
    with open(ssl_conf, "a") as file:
        file.write("\n[SAN]\n")
        file.write("subjectAltName=DNS:%s" % ",DNS:".join(domains))

    proc = subprocess.Popen(
        ["openssl", "req", "-new", "-sha256",
         "-key", "%s/%s.key" % (crt_dir, name),
         "-subj", "/",
         "-reqexts", "SAN",
         "-config", ssl_conf],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)

    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))

    with open("%s/%s.csr" % (crt_dir, name), "wb") as file:
        file.write(out)


def exist_crt(name):
    return os.path.isfile("%s/%s.crt" % (crt_dir, name))


def create_crt(name):
    if exist_crt(name):
        shutil.copyfile(
            "%s/%s.crt" % (crt_dir, name),
            "%s/%s_%s.crt" % (crt_bak_dir,
                              datetime.now().strftime("%Y%m%d_%H%M%S"),
                              name))

    signed_crt = acme_tiny.get_crt("config/account.key",
                            "%s/%s.csr" % (crt_dir, name),
                            acme_challenge_dir,
                            logger,
                            acme_ca)

    if chained_crt == "true":
        signed_crt += urlopen(acme_intermediate).read().decode("utf8")

    with open("%s/%s.crt" % (crt_dir, name), "w") as file:
        file.write(signed_crt)


def notify_container(container_list):
    if not container_list:
        return

    for container in container_list.strip(',').split(","):
        logger.info("Send SIGHUP to " + container)

        proc = subprocess.Popen(
            ["docker", "kill",
             "-s", "SIGHUP",
             container],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)

        out, err = proc.communicate()
        if proc.returncode != 0:
            logger.error("Docker Error: {0}".format(err))


def update_dhparam():
    if not dh_max_age:
        return

    dh_param_file = "%s/dhparam.pem" % crt_dir

    if os.path.isfile(dh_param_file) and \
       (datetime.now()-datetime.fromtimestamp(os.path.getmtime(dh_param_file))).days < int(dh_max_age):
        return

    logger.info("Create DH param file")

    proc = subprocess.Popen(
        ["openssl", "dhparam",
         "-out", dh_param_file,
         "2048"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)

    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))

# Program Start

os.makedirs(crt_dir, exist_ok=True)
os.makedirs(crt_bak_dir, exist_ok=True)
os.makedirs(tmp_dir, exist_ok=True)
os.makedirs(acme_challenge_dir, exist_ok=True)

if not os.path.isfile("config/account.key"):
    proc = subprocess.Popen(
        ["openssl", "genrsa", "4096"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE)

    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))

    with open("config/account.key", "wb") as file:
        file.write(out)

logger.info("Docker ACME started")
while True:
    # check dh param
    update_dhparam()

    # get predefined certs and notifies from variables
    certs = {}
    notifies = {}
    for key, value in dict(os.environ).items():
        key = key.lower()
        if not key.startswith("cert_"):
            continue

        certs[key[5:]] = sorted(filter(len, set(value.split(","))))

    # get container configurations
    try:
        config = configparser.ConfigParser()
        config.read("%s/crt_domains.ini" % tmp_dir)

        for crt in config.sections():
            certs[crt] = sorted(
                filter(len, set(config[crt]["domains"].split(","))))
            notifies[crt] = config[crt]["notify"]

    except Exception:
        pass

    changed = False
    for crt, domains in certs.items():
        try:
            if check_crt(crt, domains):
                logger.info("[%s] Certificate valid -> Skipping" % crt)
                continue

            # create key if not exist
            if not exist_key(crt):
                logger.info("[%s] Generate RSA private key" % crt)
                create_key(crt)

            # generate CSR
            logger.info("[%s] Generate CSR" % crt)
            create_csr(crt, domains)

            # get certificate
            create_crt(crt)
            changed = True

        except ValueError as e:
            logger.error(e)
        except IOError as e:
            logger.error(e)

    # is a cert changed notify containers
    if changed:
        # Notify default containers
        logger.info("[General] Notify containers")
        notify_container(container_notify)
        # Notify certificate based containers
        for crt, containers in notifies.items():
            logger.info("[%s] Notify containers" % crt)
            notify_container(containers)

    # wait update every hour or if force_crt_update exist
    counter = 3600
    while counter > 0 and not os.path.isfile("%s/force_crt_update" % tmp_dir):
        time.sleep(1)
        counter -= 1

    if os.path.isfile("%s/force_crt_update" % tmp_dir):
        logger.info("Force update")
        os.remove("%s/force_crt_update" % tmp_dir)
