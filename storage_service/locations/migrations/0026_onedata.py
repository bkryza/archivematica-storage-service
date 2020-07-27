# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from __future__ import absolute_import
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("locations", "0025_update_package_size")]

    operations = [
        migrations.CreateModel(
            name="Onedata",
            fields=[
                (
                    "id",
                    models.AutoField(
                        verbose_name="ID",
                        serialize=False,
                        auto_created=True,
                        primary_key=True,
                    ),
                ),
                (
                    "oneprovider_host",
                    models.CharField(
                        help_text="Oneprovider host",
                        max_length=2048,
                        verbose_name="Oneprovider host",
                    ),
                ),
                (
                    "access_token",
                    models.CharField(
                        max_length=2048, verbose_name="Oneclient access token"
                    ),
                ),
                (
                    "space_name",
                    models.CharField(
                        max_length=2048,
                        verbose_name="Onedata space name",
                    ),
                ),
                (
                    "space_guid",
                    models.CharField(
                        max_length=2048,
                        verbose_name="Onedata space ID",
                    ),
                ),
                (
                    "manually_mounted",
                    models.BooleanField(
                        verbose_name="Oneclient is manually mounted",
                    ),
                ),
                (
                    "only_local_replicas",
                    models.BooleanField(
                        verbose_name="Browse only local replicas",
                    ),
                ),
                (
                    "oneclient_cli",
                    models.TextField(
                        verbose_name="Additional Oneclietn arguments",
                    ),
                ),
            ],
            options={"verbose_name": "Onedata"},
        ),
        migrations.AlterField(
            model_name="space",
            name="access_protocol",
            field=models.CharField(
                help_text="How the space can be accessed.",
                max_length=8,
                verbose_name="Access protocol",
                choices=[
                    (b"ARKIVUM", "Arkivum"),
                    (b"DV", "Dataverse"),
                    (b"DC", "DuraCloud"),
                    (b"DSPACE", "DSpace via SWORD2 API"),
                    (b"FEDORA", "FEDORA via SWORD2"),
                    (b"GPG", "GPG encryption on Local Filesystem"),
                    (b"FS", "Local Filesystem"),
                    (b"LOM", "LOCKSS-o-matic"),
                    (b"NFS", "NFS"),
                    (b"PIPE_FS", "Pipeline Local Filesystem"),
                    (b"SWIFT", "Swift"),
                    (b"S3", "S3"),
                    (b"ONEDATA", "Onedata"),
                ],
            ),
        ),
        migrations.AddField(
            model_name="onedata",
            name="space",
            field=models.OneToOneField(to="locations.Space", to_field="uuid"),
        ),
    ]
