from __future__ import absolute_import

import json
import logging
import sys

from django.dispatch import receiver, Signal
from django.contrib.auth.models import User
from django.conf import settings
from django.db.models import signals
from django.urls import reverse
from django.utils.translation import ugettext as _
from django_cas_ng.signals import cas_user_authenticated
from tastypie.models import create_api_key
from prometheus_client import Counter

LOGGER = logging.getLogger(__name__)

deletion_request = Signal(providing_args=["uuid", "location", "url", "pipeline"])
failed_fixity_check = Signal(providing_args=["uuid", "location", "report"])
successful_fixity_check = Signal(providing_args=["uuid", "location", "report"])
fixity_check_not_run = Signal(providing_args=["uuid", "location", "report"])


def _notify_administrators(subject, message):
    admin_users = User.objects.filter(is_superuser=True)
    for user in admin_users:
        try:
            user.email_user(subject, message)
        except Exception:
            LOGGER.exception("Unable to send email to %s", user.email)


@receiver(deletion_request, dispatch_uid="deletion_request")
def report_deletion_request(sender, **kwargs):
    subject = _("Deletion request for package %(uuid)s") % {"uuid": kwargs["uuid"]}
    message = (
        _(
            """A package deletion request was received:

Pipeline UUID: %(pipeline)s
Package UUID: %(uuid)s
Package location: %(location)s"""
        )
        % {
            "pipeline": kwargs["pipeline"],
            "uuid": kwargs["uuid"],
            "location": kwargs["location"],
        }
    )

    # The URL may not be configured in the site; if it isn't,
    # don't try to tell the user the URL to approve/deny the request.
    if kwargs["url"]:
        message = message + _("To approve this deletion request, visit: %(url)s") % {
            "url": kwargs["url"] + reverse("package_delete_request")
        }

    _notify_administrators(subject, message)


def _log_report(uuid, success, message=None):
    # NOTE Importing this at the top of the module fails because this file is
    # imported in models.__init__.py and seems to cause a circular import error
    from . import models

    package = models.Package.objects.get(uuid=uuid)
    models.FixityLog.objects.create(
        package=package, success=success, error_details=message
    )


@receiver(failed_fixity_check, dispatch_uid="fixity_check")
def report_failed_fixity_check(sender, **kwargs):
    report_data = json.loads(kwargs["report"])
    _log_report(kwargs["uuid"], False, report_data["message"])

    subject = _("Fixity check failed for package %(uuid)s") % {"uuid": kwargs["uuid"]}
    message = (
        _(
            """
A fixity check failed for the package with UUID %(uuid)s. This package is currently stored at: %(location)s

Full failure report (in JSON format):
%(report)s
"""
        )
        % {
            "uuid": kwargs["uuid"],
            "location": kwargs["location"],
            "report": kwargs["report"],
        }
    )

    _notify_administrators(subject, message)


@receiver(successful_fixity_check, dispatch_uid="fixity_check")
def report_successful_fixity_check(sender, **kwargs):
    _log_report(kwargs["uuid"], True)


@receiver(fixity_check_not_run, dispatch_uid="fixity_check")
def report_not_run_fixity_check(sender, **kwargs):
    """Handle a fixity not run signal."""
    report_data = json.loads(kwargs["report"])
    _log_report(uuid=kwargs["uuid"], success=None, message=report_data["message"])


def _create_api_key(sender, *args, **kwargs):
    """Create API key for every user, for TastyPie.

    We don't want to run this in our tests because our fixtures provision a
    custom key. Tell me there is a better way to do this that does not require
    more scattering of signal business.
    """
    if "pytest" in sys.modules:
        return
    create_api_key(sender, **kwargs)


signals.post_save.connect(_create_api_key, sender=User)


if settings.PROMETHEUS_ENABLED:
    # Count saves and deletes via Prometheus.
    # This is a bit of a flawed way to do it (it doesn't include bulk create,
    # update, etc), but is a good starting point.
    # django-prometheus provides these counters via a model mixin, but signals
    # are less invasive.

    model_save_count = Counter(
        "django_model_save_total", "Total model save calls", ["model"]
    )
    model_delete_count = Counter(
        "django_model_delete_total", "Total model delete calls", ["model"]
    )

    @receiver(signals.post_save)
    def increment_model_save_count(sender, **kwargs):
        model_save_count.labels(model=sender.__name__).inc()

    @receiver(signals.post_delete)
    def increment_model_delete_count(sender, **kwargs):
        model_delete_count.labels(model=sender.__name__).inc()


def _user_is_administrator(cas_attributes):
    """Determine if new user is an administrator from CAS attributes.

    :param cas_attributes: Attributes dict returned by CAS server.

    :returns: True if expected value is found, otherwise False.
    """
    ADMIN_ATTRIBUTE = settings.CAS_ADMIN_ATTRIBUTE
    ADMIN_ATTRIBUTE_VALUE = settings.CAS_ADMIN_ATTRIBUTE_VALUE
    if (ADMIN_ATTRIBUTE is None) or (ADMIN_ATTRIBUTE_VALUE is None):
        LOGGER.error(
            "Error determining if new user is an administrator. Please "
            "be sure that env variables AUTH_CAS_ADMIN_ATTRIBUTE and "
            "AUTH_CAS_ADMIN_ATTRIBUTE_VALUE are properly set."
        )
        return False

    # CAS attributes are a dictionary. The value for a given key can be
    # a string or a list, so our approach for checking for the expected
    # value takes that into account.
    ATTRIBUTE_TO_CHECK = cas_attributes.get(ADMIN_ATTRIBUTE)
    if isinstance(ATTRIBUTE_TO_CHECK, list):
        if ADMIN_ATTRIBUTE_VALUE in ATTRIBUTE_TO_CHECK:
            return True
    elif isinstance(ATTRIBUTE_TO_CHECK, str):
        if ATTRIBUTE_TO_CHECK == ADMIN_ATTRIBUTE_VALUE:
            return True
    return False


@receiver(cas_user_authenticated)
def cas_user_authenticated_callback(sender, **kwargs):
    """Set user.is_superuser based on CAS attributes.

    When a user is authenticated, django_cas_ng sends the
    cas_user_authenticated signal, which includes any attributes
    returned by the CAS server during p3/serviceValidate.

    When the CAS_CHECK_ADMIN_ATTRIBUTES setting is enabled, we use this
    receiver to set user.is_superuser to True if we find the expected
    key-value combination configured with CAS_ADMIN_ATTRIBUTE and
    CAS_ADMIN_ATTRIBUTE_VALUE in the CAS attributes, and False if not.

    This check happens for both new and existing users, so that changes
    in group membership on the CAS server (e.g. a user being added or
    removed from the administrator group) are applied in the Storage
    Service on the next login.
    """
    if not settings.CAS_CHECK_ADMIN_ATTRIBUTES:
        return

    username = kwargs.get("user")
    attributes = kwargs.get("attributes")

    if not attributes:
        return

    user = User.objects.get(username=username)
    is_administrator = _user_is_administrator(attributes)
    if user.is_superuser != is_administrator:
        user.is_superuser = is_administrator
        user.save()
