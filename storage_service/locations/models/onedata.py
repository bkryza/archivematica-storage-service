from __future__ import absolute_import

# stdlib, alphabetical
import datetime
import logging
import os
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
        if count >= 25000:
            return "25000+"

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

    res = {"directories": directories, "entries": entries, "properties": properties}

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
        LOGGER.info("Browsing Onedata path: %s", path)

        self.mount()

        return generate_directory_tree(path, self.only_local_replicas)

    def move_to_storage_service(self, src_path, dest_path, dest_space):
        """ Moves src_path to dest_space.staging_path/dest_path. """
        LOGGER.info("Moving file from {} to storage path {} and space {}".format(
            src_path, dest_path, dest_space))

        self.mount()

        self.space.create_local_directory(dest_path)
        return self.space.move_rsync(src_path, dest_path)

    def move_from_storage_service(self, source_path, destination_path, package=None):
        """ Moves self.staging_path/src_path to dest_path. """
        LOGGER.info("Moving file from storage {} to path {}".format(
            source_path, destination_path))

        self.mount()

        self.space.create_local_directory(destination_path)
        return self.space.move_rsync(source_path, destination_path, try_mv_local=False)

    def save(self, *args, **kwargs):
        if not self.space.path:
            raise StorageError("Oneclient mountpoint must not be empty")

        if self.space.path == "/tmp/oneclient":
            raise StorageError("Path must be different than /tmp/oneclient")

        super(Onedata, self).save(*args, **kwargs)

    def client_mountpoint(self):
        """Generate Oneclient mountpoint path."""
        return self.space.path

    def unmount(self):
        """Unmount Oneclient."""
        LOGGER.info("Unmounting Oneclient")

        result = subprocess.check_output(["fusermount", "-uz", self.client_mountpoint()], timeout=10)

    def mount(self):
        """Mount the Oneclient."""
        LOGGER.debug("Checking if oneclient is mounted")

        mountpoint = self.client_mountpoint()

        # If the mountpoint does not exist, create it
        if not os.path.ismount(mountpoint):

            if not os.path.exists(mountpoint):
                os.makedirs(mountpoint)

            oneclient_command = ["oneclient",
                                 "-t", self.access_token,
                                 "-H", self.oneprovider_host,
                                 "--space", self.space_name]

            if self.oneclient_cli:
                oneclient_command.extend(oneclient_cli.split())

            oneclient_command += [mountpoint]

            LOGGER.info("Mounting Oneclient using command: " +
                        str(' '.join(oneclient_command)))

            result = subprocess.check_output(oneclient_command, timeout=30)

            LOGGER.info("Oneclient mounted successfully")

        # If the mountpoint exists, but does not respond, remount it again
        try:
            result = subprocess.check_output(["ls", mountpoint], timeout=10)
            LOGGER.info("Oneclient is online")
        except TimeoutExpired as e:
            LOG.info("Remounting unresponsive Oneclient mountpoint")
            self.unmount()
            self.mount()
