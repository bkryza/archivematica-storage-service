from __future__ import absolute_import

# stdlib, alphabetical
import base64
import datetime
import logging
import os
import requests
import subprocess

# Core Django, alphabetical
from django.db import models
from django.utils.translation import ugettext_lazy as _

# Third party dependencies, alphabetical
from xattr import xattr

# This project, alphabetical
from common import utils
import scandir

# This module, alphabetical
from .location import Location

LOGGER = logging.getLogger(__name__)

ONEDATA_REPLICATION_PROGRESS = "org.onedata.replication_progress"
ONEDATA_BLOCKS_COUNT = "org.onedata.file_blocks_count"


def is_local_replica(path):
    """Check if a file is completely replicated on the current
    Oneprovider instance."""
    try:
        xa = xattr(path)
        replication_progress = xa.get(ONEDATA_REPLICATION_PROGRESS)
        blocks_count = xa.get(ONEDATA_BLOCKS_COUNT)
    except IOError as e:
        LOGGER.error("%s does not have Onedata extended attributes", path)
        return False

    LOGGER.debug("%s replication_progress=%s blocks=%s",
                path, replication_progress, blocks_count)

    return replication_progress == "100%" and blocks_count == "1"


def read_directory(path, only_local_replicas):
    """Generate all directory entries, excluding hidden files."""
    for entry in scandir.scandir(path):
        if only_local_replicas:
            if is_local_replica(entry.path):
                yield entry
        else:
            yield entry


def scan_directories_deep(path, only_local_replicas):
    """Traverse recursively through a directory."""
    LOGGER.info("Scanning directory: %s", path)

    try:
        for entry in scandir.scandir(path):
            if entry.is_dir():
                for subentry in scan_directories_and_filter(entry.path, only_local_replicas):
                    LOGGER.debug("Returning subentry: %s", subentry)
                    yield subentry
            else:
                if only_local_replicas and not is_local_replica(entry.path):
                    LOGGER.debug("%s is not replicated locally", entry.path)
                    continue
                else:
                    LOGGER.debug("Returning entry: %s", entry)
                    yield entry
    except OSError:
        raise StopIteration()


def count_objects(path, only_local_replicas):
    """Counts all files in a directory and its subdirectories."""
    LOGGER.info("Counting objects at path: %s", path)

    count = 0
    for entry in scan_directories_and_filter(path, only_local_replicas):
        count += 1

        # Limit the number of files
        if count >= 5000:
            return "5000+"

    return count


def generate_directory_tree(path, only_local_replicas):
    """Traverse Onedata space or subdirectory and return dictionary
    with corresponding entries.
    """

    if only_local_replicas:
        # Disable counting if we are already filtering local replicas
        should_count = False
    else:
        should_count = utils.get_setting("object_counting_disabled", False)

    entries = []
    directories = []
    properties = {}

    for entry in sorted(read_directory(path, False), key=lambda e: e.name.lower()):
        entries.append(entry.name)
        if not entry.is_dir():
            properties[entry.name] = {"size": entry.stat().st_size}
        elif os.access(entry.path, os.R_OK):
            directories.append(entry.name)
            if should_count:
                properties[entry.name] = {
                    "object count": count_objects(entry.path, only_local_replicas)
                }

    res = {"directories": directories,
        "entries": entries, "properties": properties}

    LOGGER.debug("Returning result for %s: %s", path, res)

    return res


class Onedata(models.Model):
    """Spaces accessed over Onedata."""

    space = models.OneToOneField("Space", to_field="uuid")

    # Space.path is the local path
    oneprovider_host = models.CharField(
        max_length=2048,
        verbose_name=_("Oneprovider host"),
        help_text=_("Hostname of the Oneprovider host."),
    )
    access_token = models.CharField(
        max_length=2048,
        verbose_name=_("Access token"),
        help_text=_("Access token."),
    )
    space_name = models.CharField(
        max_length=2048,
        verbose_name=_("Onedata space name"),
        help_text=_("Name of the Onedata space where the local replicas of "
                    "data to be archived are available."),
        blank=True
    )
    space_guid = models.CharField(
        max_length=2048,
        verbose_name=_("Onedata space ID"),
        help_text=_("ID of the Onedata space where the local replicas of "
                    "data to be archived are available."),
        blank=True
    )
    oneclient_rest_endpoint = models.CharField(
        max_length=2048,
        verbose_name=_("Oneclient REST endpoint"),
        help_text=_("Endpoint of REST API for mounting Oneclient on K8S"),
        blank=True
    )
    manually_mounted = models.BooleanField(
        verbose_name=_("Oneclient manually mounted"),
        help_text=_("Oneclient has been already manually mounted, "
                       "do not try to mount it automatically"), default=False
    )
    only_local_replicas = models.BooleanField(
        verbose_name=_("Browse only local replicas"),
        help_text=_("Browser should only show files which are fully "
                    "replicated to this Oneprovider storage."), default=False
    )
    oneclient_cli = models.TextField(
        verbose_name=_("Oneclient command line arguments"),
        help_text=_("Additional list of Oneclient command line arguments."),
        blank=True
    )

    class Meta:
        verbose_name = _("Onedata")
        app_label = "locations"

    ALLOWED_LOCATION_PURPOSE = [
        Location.TRANSFER_SOURCE
    ]

    def browse(self, path, *args, **kwargs):
        path = path + ".__onedata_archivematica"

        LOGGER.info("Browsing Onedata path: %s", path)

        self.mount()

        return generate_directory_tree(path, self.only_local_replicas)

    def move_to_storage_service(self, src_path_, dest_path_, dest_space):
        """ Moves src_path to dest_space.staging_path/dest_path. """
        src_path = os.path.normpath(src_path_) + ".__onedata_archivematica"
        dest_path = os.path.normpath(dest_path_)

        LOGGER.info("move_to_storage_service: Syncing files from {} to storage path {} and space {}".format(
            src_path, dest_path, dest_space))

        self.mount()

        LOGGER.info("Creating local directory: {}".format(dest_path))
        self.space.create_local_directory(dest_path)
        if not os.path.isdir(dest_path):
            LOGGER.info("Failed to create local directory - retrying")
            try:
                os.makedirs(dest_path)
            except Exception as e:
                LOGGER.error(
                    "Failed to create local directory {} because {}".format(dest_path, str(e)))

        for root, dirs, files in os.walk(src_path):
            LOGGER.info("Traversing directory {}".format(root))
            rel_root = os.path.relpath(root, src_path)
            for d in dirs:
                dest_dir = os.path.normpath(os.path.join(
                    dest_path, os.path.join(rel_root, d)))
                LOGGER.info("Creating directory {}".format(dest_dir))
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)
            for f in files:
                dest_file = os.path.normpath(os.path.join(
                    dest_path, os.path.join(rel_root, f)))
                LOGGER.info("Symlinking source file {} to {}".format(
                    os.path.join(root, f), dest_file))
                try:
                    os.symlink(os.path.join(root, f), dest_file)
                    if not os.path.islink(dest_file):
                        LOGGER.error("Failed to create symlink: {}", dest_file)
                except Exception as e:
                    LOGGER.error(
                        "Failed to create local symlink {} because {}".format(dest_file, str(e)))

        # return self.space.move_rsync(src_path, dest_path)

    def move_from_storage_service(self, src_path_, dest_path_, package=None):
        """ Moves self.staging_path/src_path to dest_path. """
        src_path = os.path.normpath(src_path_)
        dest_path = os.path.normpath(dest_path_)

        LOGGER.info("move_from_storage_service: Moving file from storage {} to path {}".format(
            src_path, dest_path))

        self.mount()

        self.space.create_local_directory(dest_path)
        for root, dirs, files in os.walk(src_path):
            LOGGER.info("Traversing directory {}".format(root))
            rel_root = os.path.relpath(root, src_path)
            for d in dirs:
                dest_dir = os.path.join(dest_path, os.path.join(rel_root, d))
                LOGGER.info("Creating directory {}".format(dest_dir))
                if not os.path.exists(dest_dir):
                    os.makedirs(dest_dir)
            for f in files:
                dest_file = os.path.join(dest_path, os.path.join(rel_root, f))
                LOGGER.info("Symlinking source file {} to {}".format(
                    os.path.join(root, f), dest_file))
                os.symlink(os.path.join(root, f), dest_file)

        # return self.space.move_rsync(source_path, destination_path, try_mv_local=False)

    def save(self, *args, **kwargs):
        if not self.space.path:
            raise StorageError("Oneclient mountpoint must not be empty")

        if self.space.path == "/tmp/oneclient":
            raise StorageError("Path must be different than /tmp/oneclient")

        super(Onedata, self).save(*args, **kwargs)

    def client_mountpoint(self):
        """Generate Oneclient mountpoint path."""
        return self.space.path


    def execute_in_pod(self, endpoint, command):
        """Mount Oneclient in K8S pod."""

        command_string = str(' '.join(command))
        b64 = base64.b64encode(command_string)

        LOGGER.debug("Executing command in pod '{}'".format(command_string))
        request = "{}/exec?cmd={}".format(endpoint, b64)
        LOGGER.debug("Executing REST call {}".format(request))
        response = requests.get(request, timeout=15)

        exit_code = int(response.headers['X-Shell2http-Exit-Code'])
        body = response.text

        if exit_code != 0:
            LOGGER.error("Failed executing command on {} with exit code {}".format(
                endpoint, exit_code))
        else:
            LOGGER.info("Command executed succesfully")

        return exit_code, body


    def mount(self):
        """Mount the Oneclient."""
        LOGGER.debug("Checking if oneclient is mounted")

        mountpoint = self.client_mountpoint()

        oneclient_command = ["oneclient",
                             "--enable-archivematica",
                             "--force-proxy-io",
                             "-o", "allow_other",
                             "-t", self.access_token,
                             "-H", self.oneprovider_host,
                             "--space", self.space_name]

        if self.oneclient_cli:
            oneclient_command.extend(oneclient_cli.split())

        oneclient_command += [mountpoint]

        if self.oneclient_rest_endpoint:
            try:
                exit_code, body = self.execute_in_pod(
                        self.oneclient_rest_endpoint, ["ls", mountpoint])
                if exit_code == 0 and self.space_name in body:
                    LOGGER.info("Oneclient already mounted")
                else:
                    LOGGER.info("Oneclient is not mounted - remounting")
                    self.execute_in_pod(
                            self.oneclient_rest_endpoint,
                            ["mkdir", "-p", mountpoint])
                    self.execute_in_pod(
                            self.oneclient_rest_endpoint,
                            ["fusermount", "-uz", mountpoint])
                    self.execute_in_pod(
                            self.oneclient_rest_endpoint, oneclient_command)
            except Exception as e:
                LOGGER.error("Failed mounting oneclient using {}: {}".format(
                    self.oneclient_rest_endpoint, str(e)))
        else:
            if not os.path.ismount(mountpoint):
                # If the mountpoint does not exist, create it
                if not os.path.exists(mountpoint):
                    os.makedirs(mountpoint)
                try:
                    LOGGER.info("Mounting Oneclient using command: " + str(' '.join(oneclient_command)))

                    result = subprocess.check_output(
                            ["ls", mountpoint], timeout=10)
                    LOGGER.info("Oneclient is online")

                except TimeoutExpired as e:
                    LOG.info("Remounting unresponsive Oneclient mountpoint")
                    subprocess.check_output(["fusermount", "-uz", self.mountpoint], timeout=10)
                    self.mount()
