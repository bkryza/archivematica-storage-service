from __future__ import absolute_import

# stdlib, alphabetical
import datetime
import os

# Core Django, alphabetical
from django.db import models
from django.utils.translation import ugettext_lazy as _

# Third party dependencies, alphabetical

# This project, alphabetical

# This module, alphabetical
from .location import Location


class Onedata(models.Model):
    """ Spaces accessed over Onedata. """

    space = models.OneToOneField("Space", to_field="uuid")

    # Space.path is the local path
    oneprovider_host = models.TextField(
        verbose_name=_("Oneprovider host"),
        help_text=_("Hostname of the Oneprovider host."),
    )
    access_token = models.TextField(
        verbose_name=_("Access token"),
        help_text=_("Access token."),
    )
    space_name = models.TextField(
        verbose_name=_("Onedata space name"),
        help_text=_("Name of the Onedata space where the local replicas of "
                    "data to be archived are available.")

    class Meta:
        verbose_name = _("Onedata")
        app_label = "locations"

    ALLOWED_LOCATION_PURPOSE = [
        Location.TRANSFER_SOURCE
    ]

    def move_to_storage_service(self, src_path, dest_path, dest_space):
        """ Moves src_path to dest_space.staging_path/dest_path. """
        pass
        #self.space.create_local_directory(dest_path)
        #return self.space.move_rsync(src_path, dest_path)

    def move_from_storage_service(self, source_path, destination_path, package=None):
        """ Moves self.staging_path/src_path to dest_path. """
        pass
        #self.space.create_local_directory(destination_path)
        #return self.space.move_rsync(source_path, destination_path, try_mv_local=True)

    def save(self, *args, **kwargs):
        self.verify()
        super(NFS, self).save(*args, **kwargs)

    def verify(self):
        """ Verify that the space is accessible to the storage service. """
        # TODO run script to verify that it works
        pass
        # if self.manually_mounted:
            # verified = os.path.ismount(self.space.path)
            # self.space.verified = verified
            # self.space.last_verified = datetime.datetime.now()

    def mount(self):
        """ Mount the Oneclient. """
        # sudo mount -t nfs -o proto=tcp,port=2049 192.168.1.133:/export /mnt/
        # sudo mount -t self.version -o proto=tcp,port=2049 self.remote_name:self.remote_path self.space.path
        # or /etc/fstab
        # self.remote_name:self.remote_path   self.space.path   self.version    auto,user  0  0
        # may need to tweak options
        pass
