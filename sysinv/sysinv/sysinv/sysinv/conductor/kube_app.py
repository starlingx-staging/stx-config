# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright (c) 2018-2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# All Rights Reserved.
#

""" System Inventory Kubernetes Application Operator."""

import base64
import copy
import docker
import grp
import functools
import keyring
import os
import pwd
import re
import ruamel.yaml as yaml
import shutil
import six
import subprocess
import threading
import time

from collections import namedtuple
from eventlet import greenpool
from eventlet import greenthread
from eventlet import queue
from eventlet import Timeout
from fm_api import constants as fm_constants
from fm_api import fm_api
from oslo_config import cfg
from oslo_log import log as logging
from sysinv.api.controllers.v1 import kube_app
from sysinv.common import constants
from sysinv.common import exception
from sysinv.common import kubernetes
from sysinv.common import utils as cutils
from sysinv.common.storage_backend_conf import K8RbdProvisioner
from sysinv.conductor import openstack
from sysinv.helm import common
from sysinv.helm import helm
from sysinv.helm import utils as helm_utils
from sysinv.openstack.common.gettextutils import _


# Log and config
LOG = logging.getLogger(__name__)
kube_app_opts = [
    cfg.StrOpt('armada_image_tag',
               default=('quay.io/airshipit/armada:'
                        '8a1638098f88d92bf799ef4934abe569789b885e-ubuntu_bionic'),
               help='Docker image tag of Armada.'),
                ]
CONF = cfg.CONF
CONF.register_opts(kube_app_opts)


# Constants
APPLY_SEARCH_PATTERN = 'Processing Chart,'
ARMADA_CONTAINER_NAME = 'armada_service'
ARMADA_MANIFEST_APPLY_SUCCESS_MSG = 'Done applying manifest'
ARMADA_RELEASE_ROLLBACK_FAILURE_MSG = 'Error while rolling back tiller release'
CONTAINER_ABNORMAL_EXIT_CODE = 137
DELETE_SEARCH_PATTERN = 'Deleting release'
ROLLBACK_SEARCH_PATTERN = 'Helm rollback of release'
INSTALLATION_TIMEOUT = 3600
MAX_DOWNLOAD_THREAD = 5
MAX_DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_WAIT_BEFORE_RETRY = 30
TARFILE_DOWNLOAD_CONNECTION_TIMEOUT = 60
TARFILE_TRANSFER_CHUNK_SIZE = 1024 * 512
DOCKER_REGISTRY_USER = 'admin'
DOCKER_REGISTRY_SERVICE = 'CGCS'
DOCKER_REGISTRY_SECRET = 'default-registry-key'
CHARTS_PENDING_INSTALL_ITERATIONS = 60

ARMADA_HOST_LOG_LOCATION = '/var/log/armada'
ARMADA_CONTAINER_LOG_LOCATION = '/logs'
ARMADA_LOCK_GROUP = 'armada.process'
ARMADA_LOCK_VERSION = 'v1'
ARMADA_LOCK_NAMESPACE = 'kube-system'
ARMADA_LOCK_PLURAL = 'locks'
ARMADA_LOCK_NAME = 'lock'


# Helper functions
def generate_armada_manifest_filename(app_name, app_version, manifest_filename):
    return os.path.join('/manifests', app_name, app_version,
                        app_name + '-' + manifest_filename)


def generate_manifest_filename_abs(app_name, app_version, manifest_filename):
    return os.path.join(constants.APP_INSTALL_PATH,
                        app_name, app_version, manifest_filename)


def generate_images_filename_abs(armada_mfile_dir, app_name):
    return os.path.join(armada_mfile_dir, app_name + '-images.yaml')


def generate_overrides_dir(app_name, app_version):
    return os.path.join(common.HELM_OVERRIDES_PATH, app_name, app_version)


def create_app_path(path):
    uid = pwd.getpwnam(constants.SYSINV_USERNAME).pw_uid
    gid = os.getgid()

    if not os.path.exists(constants.APP_INSTALL_PATH):
        os.makedirs(constants.APP_INSTALL_PATH)
        os.chown(constants.APP_INSTALL_PATH, uid, gid)

    os.makedirs(path)
    os.chown(path, uid, gid)


def get_app_install_root_path_ownership():
    uid = os.stat(constants.APP_INSTALL_ROOT_PATH).st_uid
    gid = os.stat(constants.APP_INSTALL_ROOT_PATH).st_gid
    return (uid, gid)


def get_local_docker_registry_auth():
    registry_password = keyring.get_password(
        DOCKER_REGISTRY_SERVICE, DOCKER_REGISTRY_USER)
    if not registry_password:
        raise exception.DockerRegistryCredentialNotFound(
            name=DOCKER_REGISTRY_USER)

    return dict(username=DOCKER_REGISTRY_USER,
                password=registry_password)


Chart = namedtuple('Chart', 'metadata_name name namespace location release labels sequenced')


class AppOperator(object):
    """Class to encapsulate Kubernetes App operations for System Inventory"""

    APP_OPENSTACK_RESOURCE_CONFIG_MAP = 'ceph-etc'
    # List of in progress apps and their abort status
    abort_requested = {}

    def __init__(self, dbapi):
        self._dbapi = dbapi
        self._fm_api = fm_api.FaultAPIs()
        self._docker = DockerHelper(self._dbapi)
        self._helm = helm.HelmOperator(self._dbapi)
        self._kube = kubernetes.KubeOperator(self._dbapi)
        self._utils = kube_app.KubeAppHelper(self._dbapi)
        self._lock = threading.Lock()

        if not os.path.isfile(constants.ANSIBLE_BOOTSTRAP_FLAG):
            self._clear_stuck_applications()

    def _clear_armada_locks(self):
        lock_name = "{}.{}.{}".format(ARMADA_LOCK_PLURAL,
                                      ARMADA_LOCK_GROUP,
                                      ARMADA_LOCK_NAME)
        try:
            self._kube.delete_custom_resource(ARMADA_LOCK_GROUP,
                                              ARMADA_LOCK_VERSION,
                                              ARMADA_LOCK_NAMESPACE,
                                              ARMADA_LOCK_PLURAL,
                                              lock_name)
        except Exception:
            # Best effort delete
            LOG.warning("Failed to clear Armada locks.")
            pass

    def _clear_stuck_applications(self):
        apps = self._dbapi.kube_app_get_all()
        for app in apps:
            if app.status in [constants.APP_UPLOAD_IN_PROGRESS,
                              constants.APP_APPLY_IN_PROGRESS,
                              constants.APP_UPDATE_IN_PROGRESS,
                              constants.APP_RECOVER_IN_PROGRESS,
                              constants.APP_REMOVE_IN_PROGRESS]:
                self._abort_operation(app, app.status, reset_status=True)
            else:
                continue

        # Delete the Armada locks that might have been acquired previously
        # for a fresh start. This guarantees that a re-apply, re-update or
        # a re-remove attempt following a status reset will not fail due
        # to a lock related issue.
        self._clear_armada_locks()

    def _raise_app_alarm(self, app_name, app_action, alarm_id, severity,
                         reason_text, alarm_type, repair_action,
                         service_affecting):

        entity_instance_id = "%s=%s" % (fm_constants.FM_ENTITY_TYPE_APPLICATION,
                                        app_name)
        app_alarms = self._fm_api.get_faults(entity_instance_id)
        if app_alarms:
            if ((app_action == constants.APP_APPLY_FAILURE and
                 app_alarms[0].alarm_id ==
                     fm_constants.FM_ALARM_ID_APPLICATION_APPLY_FAILED) or
                (app_action == constants.APP_UPLOAD_FAILURE and
                 app_alarms[0].alarm_id ==
                     fm_constants.FM_ALARM_ID_APPLICATION_UPLOAD_FAILED) or
                (app_action == constants.APP_REMOVE_FAILURE and
                 app_alarms[0].alarm_id ==
                     fm_constants.FM_ALARM_ID_APPLICATION_REMOVE_FAILED) or
                (app_action == constants.APP_APPLY_IN_PROGRESS and
                 app_alarms[0].alarm_id ==
                     fm_constants.FM_ALARM_ID_APPLICATION_APPLYING) or
                (app_action == constants.APP_UPDATE_IN_PROGRESS and
                 app_alarms[0].alarm_id ==
                     fm_constants.FM_ALARM_ID_APPLICATION_UPDATING)):
                # The same alarm was raised before, will re-raise to set the
                # latest timestamp.
                pass
            else:
                # Clear existing alarm for this app if it differs than the one to
                # be raised.
                self._fm_api.clear_fault(app_alarms[0].alarm_id,
                                         app_alarms[0].entity_instance_id)
        fault = fm_api.Fault(
                alarm_id=alarm_id,
                alarm_state=fm_constants.FM_ALARM_STATE_SET,
                entity_type_id=fm_constants.FM_ENTITY_TYPE_APPLICATION,
                entity_instance_id=entity_instance_id,
                severity=severity,
                reason_text=reason_text,
                alarm_type=alarm_type,
                probable_cause=fm_constants.ALARM_PROBABLE_CAUSE_UNKNOWN,
                proposed_repair_action=repair_action,
                service_affecting=service_affecting)

        self._fm_api.set_fault(fault)

    def _clear_app_alarm(self, app_name):
        entity_instance_id = "%s=%s" % (fm_constants.FM_ENTITY_TYPE_APPLICATION,
                                        app_name)
        app_alarms = self._fm_api.get_faults(entity_instance_id)
        if app_alarms:
            # There can only exist one alarm per app
            self._fm_api.clear_fault(app_alarms[0].alarm_id,
                                     app_alarms[0].entity_instance_id)

    def _register_app_abort(self, app_name):
        with self._lock:
            AppOperator.abort_requested[app_name] = False
        LOG.info("Register the initial abort status of app %s" % app_name)

    def _deregister_app_abort(self, app_name):
        with self._lock:
            try:
                del AppOperator.abort_requested[app_name]
            except KeyError:
                pass
        LOG.info("Deregister the abort status of app %s" % app_name)

    @staticmethod
    def is_app_aborted(app_name):
        try:
            return AppOperator.abort_requested[app_name]
        except KeyError:
            return False

    def _set_abort_flag(self, app_name):
        with self._lock:
            AppOperator.abort_requested[app_name] = True
        LOG.info("Abort set for app %s" % app_name)

    def _cleanup(self, app, app_dir=True):
        """" Remove application directories and override files """
        try:
            if os.path.exists(app.overrides_dir):
                shutil.rmtree(app.overrides_dir)
                if app_dir:
                    shutil.rmtree(os.path.dirname(
                        app.overrides_dir))

            if os.path.exists(app.armada_mfile_dir):
                shutil.rmtree(app.armada_mfile_dir)
                if app_dir:
                    shutil.rmtree(os.path.dirname(
                        app.armada_mfile_dir))

            if os.path.exists(app.path):
                shutil.rmtree(app.path)
                if app_dir:
                    shutil.rmtree(os.path.dirname(
                        app.path))
        except OSError as e:
            LOG.error(e)
            raise

    def _update_app_status(self, app, new_status=None, new_progress=None):
        """ Persist new app status """

        if new_status is None:
            new_status = app.status

        with self._lock:
            app.update_status(new_status, new_progress)

    def _abort_operation(self, app, operation,
                         progress=constants.APP_PROGRESS_ABORTED,
                         user_initiated=False, reset_status=False):
        if user_initiated:
            progress = constants.APP_PROGRESS_ABORTED_BY_USER

        if app.status == constants.APP_UPLOAD_IN_PROGRESS:
            new_status = constants.APP_UPLOAD_FAILURE
            op = 'application-upload'
            self._raise_app_alarm(
                app.name, constants.APP_UPLOAD_FAILURE,
                fm_constants.FM_ALARM_ID_APPLICATION_UPLOAD_FAILED,
                fm_constants.FM_ALARM_SEVERITY_WARNING,
                _("Application Upload Failure"),
                fm_constants.FM_ALARM_TYPE_3,
                _("Check system inventory log for cause."),
                False)
        elif (app.status == constants.APP_APPLY_IN_PROGRESS or
              app.status == constants.APP_UPDATE_IN_PROGRESS or
              app.status == constants.APP_RECOVER_IN_PROGRESS):
            new_status = constants.APP_APPLY_FAILURE
            if reset_status:
                if app.status == constants.APP_APPLY_IN_PROGRESS:
                    op = 'application-apply'
                else:
                    op = 'application-update'

                if app.name in constants.HELM_APPS_PLATFORM_MANAGED:
                    # For platform core apps, set the new status
                    # to 'uploaded'. The audit task will kick in with
                    # all its pre-requisite checks before reapplying.
                    new_status = constants.APP_UPLOAD_SUCCESS
                    self._clear_app_alarm(app.name)

            if (not reset_status or
                    app.name not in constants.HELM_APPS_PLATFORM_MANAGED):
                self._raise_app_alarm(
                    app.name, constants.APP_APPLY_FAILURE,
                    fm_constants.FM_ALARM_ID_APPLICATION_APPLY_FAILED,
                    fm_constants.FM_ALARM_SEVERITY_MAJOR,
                    _("Application Apply Failure"),
                    fm_constants.FM_ALARM_TYPE_3,
                    _("Retry applying the application. If the issue persists, "
                      "please check system inventory log for cause."),
                    True)
        elif app.status == constants.APP_REMOVE_IN_PROGRESS:
            new_status = constants.APP_REMOVE_FAILURE
            op = 'application-remove'
            self._raise_app_alarm(
                app.name, constants.APP_REMOVE_FAILURE,
                fm_constants.FM_ALARM_ID_APPLICATION_REMOVE_FAILED,
                fm_constants.FM_ALARM_SEVERITY_MAJOR,
                _("Application Remove Failure"),
                fm_constants.FM_ALARM_TYPE_3,
                _("Retry removing the application. If the issue persists, "
                  "please check system inventory log for cause."),
                True)
        else:
            # Should not get here, perhaps a new status was introduced?
            LOG.error("No abort handling code for app status = '%s'!" % app.status)
            return

        if not reset_status:
            self._update_app_status(app, new_status, progress)
            if not user_initiated:
                LOG.error("Application %s aborted!." % operation)
            else:
                LOG.info("Application %s aborted by user!." % operation)
        else:
            LOG.info("Resetting status of app %s from '%s' to '%s' " %
                     (app.name, app.status, new_status))
            error_msg = "Unexpected process termination while " + op +\
                        " was in progress. The application status " +\
                        "has changed from \'" + app.status +\
                        "\' to \'" + new_status + "\'."
            values = {'progress': error_msg, 'status': new_status}
            self._dbapi.kube_app_update(app.id, values)

    def _download_tarfile(self, app):
        from six.moves.urllib.request import urlopen
        from six.moves.urllib.error import HTTPError
        from six.moves.urllib.error import URLError
        from socket import timeout as socket_timeout
        from six.moves.urllib.parse import urlsplit

        def _handle_download_failure(reason):
            raise exception.KubeAppUploadFailure(
                name=app.name,
                version=app.version,
                reason=reason)

        try:
            remote_file = urlopen(
                app.tarfile, timeout=TARFILE_DOWNLOAD_CONNECTION_TIMEOUT)
            try:
                remote_filename = remote_file.info()['Content-Disposition']
            except KeyError:
                remote_filename = os.path.basename(
                    urlsplit(remote_file.url).path)

            filename_avail = True if (remote_filename is None or
                                      remote_filename == '') else False

            if filename_avail:
                if (not remote_filename.endswith('.tgz') and
                        not remote_filename.endswith('.tar.gz')):
                    reason = app.tarfile + ' has unrecognizable tar file ' + \
                        'extension. Supported extensions are: .tgz and .tar.gz.'
                    _handle_download_failure(reason)
                    return None

                filename = '/tmp/' + remote_filename
            else:
                filename = '/tmp/' + app.name + '.tgz'

            with open(filename, 'wb') as dest:
                shutil.copyfileobj(remote_file, dest, TARFILE_TRANSFER_CHUNK_SIZE)
            return filename

        except HTTPError as err:
            LOG.error(err)
            reason = 'failed to download tarfile ' + app.tarfile + \
                     ', error code = ' + str(err.code)
            _handle_download_failure(reason)
        except URLError as err:
            LOG.error(err)
            reason = app.tarfile + ' is unreachable.'
            _handle_download_failure(reason)
        except shutil.Error as err:
            LOG.error(err)
            err_file = os.path.basename(filename) if filename_avail else app.tarfile
            reason = 'failed to process tarfile ' + err_file
            _handle_download_failure(reason)
        except socket_timeout as e:
            LOG.error(e)
            reason = 'failed to download tarfile ' + app.tarfile + \
                     ', connection timed out.'
            _handle_download_failure(reason)

    def _extract_tarfile(self, app):
        def _handle_extract_failure(
                reason='failed to extract tarfile content.'):
            raise exception.KubeAppUploadFailure(
                name=app.name,
                version=app.version,
                reason=reason)

        orig_uid, orig_gid = get_app_install_root_path_ownership()

        try:
            # One time set up of base armada manifest path for the system
            if not os.path.isdir(constants.APP_SYNCED_DATA_PATH):
                os.makedirs(constants.APP_SYNCED_DATA_PATH)

            if not os.path.isdir(app.armada_mfile_dir):
                os.makedirs(app.armada_mfile_dir)

            if not os.path.isdir(app.path):
                create_app_path(app.path)

            # Temporarily change /scratch group ownership to sys_protected
            os.chown(constants.APP_INSTALL_ROOT_PATH, orig_uid,
                     grp.getgrnam(constants.SYSINV_SYSADMIN_GRPNAME).gr_gid)

            # Extract the tarfile as sysinv user
            if not cutils.extract_tarfile(app.path, app.tarfile, demote_user=True):
                _handle_extract_failure()

            if app.downloaded_tarfile:
                name, version, patches = self._utils._verify_metadata_file(
                    app.path, app.name, app.version)
                if (name != app.name or version != app.version):
                    # Save the official application info. They will be
                    # persisted in the next status update
                    app.regenerate_application_info(name, version, patches)

                if not cutils.verify_checksum(app.path):
                    _handle_extract_failure('checksum validation failed.')
                mname, mfile = self._utils._find_manifest_file(app.path)
                # Save the official manifest file info. They will be persisted
                # in the next status update
                app.regenerate_manifest_filename(mname, os.path.basename(mfile))
            else:
                name, version, patches = cutils.find_metadata_file(
                    app.path, constants.APP_METADATA_FILE)
                app.patch_dependencies = patches

            self._utils._extract_helm_charts(app.path)

        except exception.SysinvException as e:
            _handle_extract_failure(str(e))
        except OSError as e:
            LOG.error(e)
            _handle_extract_failure()
        finally:
            os.chown(constants.APP_INSTALL_ROOT_PATH, orig_uid, orig_gid)

    def _get_image_tags_by_path(self, path):
        """ Mine the image tags from values.yaml files in the chart directory,
            intended for custom apps.

            TODO(awang): Support custom apps to pull images from local registry
        """

        def _parse_charts():
            ids = []
            image_tags = []
            for r, f in cutils.get_files_matching(path, 'values.yaml'):
                with open(os.path.join(r, f), 'r') as value_f:
                    try_image_tag_repo_format = False
                    y = yaml.safe_load(value_f)
                    try:
                        ids = y["images"]["tags"].values()
                    except (AttributeError, TypeError, KeyError):
                        try_image_tag_repo_format = True

                    if try_image_tag_repo_format:
                        try:
                            y_image = y["image"]
                            y_image_tag = y_image['repository'] + ":" + y_image['tag']
                            ids = [y_image_tag]
                        except (AttributeError, TypeError, KeyError):
                            pass
                image_tags.extend(ids)
            return image_tags

        image_tags = _parse_charts()

        return list(set(image_tags))

    def _get_image_tags_by_charts(self, app_images_file, app_manifest_file, overrides_dir):
        """ Mine the image tags for charts from the images file. Add the
            image tags to the manifest file if the image tags from the charts
            do not exist in both overrides file and manifest file. Convert
            the image tags in the manifest file. Intended for system app.

            The image tagging conversion(local docker registry address prepended):
            ${LOCAL_REGISTRY_SERVER}:${REGISTRY_PORT}/<image-name>
            (ie..registry.local:9001/docker.io/mariadb:10.2.13)
        """

        manifest_image_tags_updated = False
        image_tags = []

        if os.path.exists(app_images_file):
            with open(app_images_file, 'r') as f:
                images_file = yaml.safe_load(f)

        if os.path.exists(app_manifest_file):
            with open(app_manifest_file, 'r') as f:
                charts = list(yaml.load_all(f, Loader=yaml.RoundTripLoader))

        for chart in charts:
            images_charts = {}
            images_overrides = {}
            images_manifest = {}

            overrides_image_tags_updated = False
            chart_image_tags_updated = False

            if "armada/Chart/" in chart['schema']:
                chart_data = chart['data']
                chart_name = chart_data['chart_name']
                chart_namespace = chart_data['namespace']

                # Get the image tags by chart from the images file
                if chart_name in images_file:
                    images_charts = images_file[chart_name]

                # Get the image tags from the overrides file
                overrides = chart_namespace + '-' + chart_name + '.yaml'
                app_overrides_file = os.path.join(overrides_dir, overrides)
                if os.path.exists(app_overrides_file):
                    try:
                        with open(app_overrides_file, 'r') as f:
                            overrides_file = yaml.safe_load(f)
                            images_overrides = overrides_file['data']['values']['images']['tags']
                    except (TypeError, KeyError):
                        pass

                # Get the image tags from the armada manifest file
                try_image_tag_repo_format = False
                try:
                    images_manifest = chart_data['values']['images']['tags']
                except (TypeError, KeyError, AttributeError):
                    try_image_tag_repo_format = True
                    LOG.info("Armada manifest file has no img tags for "
                             "chart %s" % chart_name)
                    pass

                if try_image_tag_repo_format:
                    try:
                        y_image = chart_data['values']['image']
                        y_image_tag = \
                            y_image['repository'] + ":" + y_image['tag']
                        images_manifest = {chart_name: y_image_tag}
                    except (AttributeError, TypeError, KeyError):
                        pass

                # For the image tags from the chart path which do not exist
                # in the overrides and manifest file, add to manifest file.
                # Convert the image tags in the overrides and manifest file
                # with local docker registry address.
                # Append the required images to the image_tags list.
                for key in images_charts:
                    if key not in images_overrides:
                        if key not in images_manifest:
                            images_manifest.update({key: images_charts[key]})
                        if not re.match(r'^.+:.+/', images_manifest[key]):
                            images_manifest.update(
                                {key: '{}/{}'.format(constants.DOCKER_REGISTRY_SERVER,
                                                     images_manifest[key])})
                            chart_image_tags_updated = True
                        image_tags.append(images_manifest[key])
                    else:
                        if not re.match(r'^.+:.+/', images_overrides[key]):
                            images_overrides.update(
                                {key: '{}/{}'.format(constants.DOCKER_REGISTRY_SERVER,
                                                     images_overrides[key])})
                            overrides_image_tags_updated = True
                        image_tags.append(images_overrides[key])

                if overrides_image_tags_updated:
                    with open(app_overrides_file, 'w') as f:
                        try:
                            overrides_file["data"]["values"]["images"] = {"tags": images_overrides}
                            yaml.safe_dump(overrides_file, f, default_flow_style=False)
                            LOG.info("Overrides file %s updated with new image tags" %
                                     app_overrides_file)
                        except (TypeError, KeyError):
                            LOG.error("Overrides file %s fails to update" %
                                      app_overrides_file)

                if chart_image_tags_updated:
                    if 'values' in chart_data:
                        chart_data['values']['images'] = {'tags': images_manifest}
                    else:
                        chart_data["values"] = {"images": {"tags": images_manifest}}
                    manifest_image_tags_updated = True

        if manifest_image_tags_updated:
            with open(app_manifest_file, 'w') as f:
                try:
                    yaml.dump_all(charts, f, Dumper=yaml.RoundTripDumper,
                                  explicit_start=True, default_flow_style=False)
                    LOG.info("Manifest file %s updated with new image tags" %
                             app_manifest_file)
                except Exception as e:
                    LOG.error("Manifest file %s fails to update with "
                              "new image tags: %s" % (app_manifest_file, e))

        return list(set(image_tags))

    def _register_embedded_images(self, app):
        """
        TODO(tngo): When we're ready to support air-gap scenario and private
        images, the following need to be done:
            a. load the embedded images
            b. tag and push them to the docker registery on the controller
            c. find image tag IDs in each chart and replace their values with
               new tags. Alternatively, document the image tagging convention
               ${LOCAL_REGISTRY_SERVER}:${REGISTRY_PORT}/<image-name>
               (e.g. registry.local:9001/prom/mysqld-exporter)
               to be referenced in the application Helm charts.
        """
        raise exception.KubeAppApplyFailure(
            name=app.name,
            version=app.version,
            reason="embedded images are not yet supported.")

    def _save_images_list(self, app):
        # Extract the list of images from the charts and overrides where
        # applicable. Save the list to the same location as the armada manifest
        # so it can be sync'ed.
        app.charts = self._get_list_of_charts(app.armada_mfile_abs)
        LOG.info("Generating application overrides...")
        self._helm.generate_helm_application_overrides(
            app.overrides_dir, app.name, mode=None, cnamespace=None,
            armada_format=True, armada_chart_info=app.charts, combined=True)
        if app.system_app:
            self._save_images_list_by_charts(app)
            # Get the list of images from the updated images overrides
            images_to_download = self._get_image_tags_by_charts(
                app.imgfile_abs, app.armada_mfile_abs, app.overrides_dir)
        else:
            # For custom apps, mine image tags from application path
            images_to_download = self._get_image_tags_by_path(app.path)

        if not images_to_download:
            # TODO(tngo): We may want to support the deployment of apps that
            # set up resources only in the future. In which case, generate
            # an info log and let it advance to the next step.
            raise exception.KubeAppUploadFailure(
                name=app.name,
                version=app.version,
                reason="charts specify no docker images.")

        with open(app.imgfile_abs, 'ab') as f:
            yaml.safe_dump({"download_images": images_to_download}, f,
                           default_flow_style=False)

    def _save_images_list_by_charts(self, app):
        # Mine the images from values.yaml files in the charts directory.
        # The list of images for each chart are saved to the images file.
        images_by_charts = {}
        for chart in app.charts:
            images = {}
            chart_name = os.path.join(app.charts_dir, chart.name)
            chart_path = os.path.join(chart_name, 'values.yaml')

            try_image_tag_repo_format = False
            if os.path.exists(chart_path):
                with open(chart_path, 'r') as f:
                    y = yaml.safe_load(f)
                    try:
                        images = y["images"]["tags"]
                    except (TypeError, KeyError, AttributeError):
                        LOG.info("Chart %s has no images tags" % chart_name)
                        try_image_tag_repo_format = True

                    if try_image_tag_repo_format:
                        try:
                            y_image = y["image"]
                            y_image_tag = \
                                y_image['repository'] + ":" + y_image['tag']
                            images = {chart.name: y_image_tag}
                        except (AttributeError, TypeError, KeyError):
                            LOG.info("Chart %s has no image tags" % chart_name)
                            pass

            if images:
                images_by_charts.update({chart.name: images})

        with open(app.imgfile_abs, 'wb') as f:
            yaml.safe_dump(images_by_charts, f, explicit_start=True,
                           default_flow_style=False)

    def _retrieve_images_list(self, app_images_file):
        with open(app_images_file, 'rb') as f:
            images_list = yaml.safe_load(f)
        return images_list

    def _download_images(self, app):
        if os.path.isdir(app.images_dir):
            return self._register_embedded_images(app)

        if app.system_app:
            # Some images could have been overwritten via user overrides
            # between upload and apply, or between applies. Refresh the
            # saved images list.
            saved_images_list = self._retrieve_images_list(app.imgfile_abs)
            saved_download_images_list = list(saved_images_list.get("download_images"))
            images_to_download = self._get_image_tags_by_charts(
                app.imgfile_abs, app.armada_mfile_abs, app.overrides_dir)
            if set(saved_download_images_list) != set(images_to_download):
                saved_images_list.update({"download_images": images_to_download})
                with open(app.imgfile_abs, 'wb') as f:
                    yaml.safe_dump(saved_images_list, f, explicit_start=True,
                                   default_flow_style=False)
        else:
            images_to_download = self._retrieve_images_list(
                app.imgfile_abs).get("download_images")

        total_count = len(images_to_download)
        threads = min(MAX_DOWNLOAD_THREAD, total_count)

        start = time.time()
        try:
            local_registry_auth = get_local_docker_registry_auth()
            with self._lock:
                self._docker._retrieve_specified_registries()
        except Exception as e:
            raise exception.KubeAppApplyFailure(
                name=app.name,
                version=app.version,
                reason=str(e))
        for idx in reversed(range(MAX_DOWNLOAD_ATTEMPTS)):
            pool = greenpool.GreenPool(size=threads)
            for tag, success in pool.imap(
                    functools.partial(self._docker.download_an_image,
                                      app.name, local_registry_auth),
                    images_to_download):
                if success:
                    continue
                if AppOperator.is_app_aborted(app.name):
                    raise exception.KubeAppApplyFailure(
                        name=app.name,
                        version=app.version,
                        reason="operation aborted by user.")
                else:
                    LOG.info("Failed to download image: %s", tag)
                    break
            else:
                with self._lock:
                    self._docker._reset_registries_info()
                elapsed = time.time() - start
                LOG.info("All docker images for application %s were successfully "
                         "downloaded in %d seconds", app.name, elapsed)
                break
            # don't sleep after last download attempt
            if idx:
                LOG.info("Retry docker images download for application %s "
                         "after %d seconds", app.name, DOWNLOAD_WAIT_BEFORE_RETRY)
                time.sleep(DOWNLOAD_WAIT_BEFORE_RETRY)
        else:
            raise exception.KubeAppApplyFailure(
                name=app.name,
                version=app.version,
                reason=constants.APP_PROGRESS_IMAGES_DOWNLOAD_FAILED)

    def _validate_helm_charts(self, app):
        failed_charts = []
        for r, f in cutils.get_files_matching(app.charts_dir, 'Chart.yaml'):
            # Eliminate redundant validation for system app
            if app.system_app and '/charts/helm-toolkit' in r:
                continue
            try:
                output = subprocess.check_output(['helm', 'lint', r])
                if "no failures" in output:
                    LOG.info("Helm chart %s validated" % os.path.basename(r))
                else:
                    LOG.error("Validation failed for helm chart %s" %
                              os.path.basename(r))
                    failed_charts.append(r)
            except Exception as e:
                raise exception.KubeAppUploadFailure(
                    name=app.name, version=app.version, reason=str(e))

        if len(failed_charts) > 0:
            raise exception.KubeAppUploadFailure(
                name=app.name, version=app.version, reason="one or more charts failed validation.")

    def _get_chart_data_from_metadata(self, app):
        """Get chart related data from application metadata

        This extracts the helm repo from the application metadata where the
        chart should be loaded.

        This also returns the list of charts that are disabled by default.

        :param app: application
        """
        repo = common.HELM_REPO_FOR_APPS
        disabled_charts = []
        lfile = os.path.join(app.path, constants.APP_METADATA_FILE)

        if os.path.exists(lfile) and os.path.getsize(lfile) > 0:
            with open(lfile, 'r') as f:
                try:
                    y = yaml.safe_load(f)
                    repo = y.get('helm_repo', common.HELM_REPO_FOR_APPS)
                    disabled_charts = y.get('disabled_charts', [])
                except KeyError:
                    pass

        LOG.info("Application %s (%s) will load charts to chart repo %s" % (
            app.name, app.version, repo))
        LOG.info("Application %s (%s) will disable charts %s by default" % (
            app.name, app.version, disabled_charts))
        return (repo, disabled_charts)

    def _upload_helm_charts(self, app):
        # Set env path for helm-upload execution
        env = os.environ.copy()
        env['PATH'] = '/usr/local/sbin:' + env['PATH']
        charts = [os.path.join(r, f)
                  for r, f in cutils.get_files_matching(app.charts_dir, '.tgz')]

        orig_uid, orig_gid = get_app_install_root_path_ownership()
        (helm_repo, disabled_charts) = self._get_chart_data_from_metadata(app)
        try:
            # Temporarily change /scratch group ownership to sys_protected
            os.chown(constants.APP_INSTALL_ROOT_PATH, orig_uid,
                     grp.getgrnam(constants.SYSINV_SYSADMIN_GRPNAME).gr_gid)
            with open(os.devnull, "w") as fnull:
                for chart in charts:
                    subprocess.check_call(['helm-upload', helm_repo, chart],
                                          env=env, stdout=fnull, stderr=fnull)
                    LOG.info("Helm chart %s uploaded" % os.path.basename(chart))

            # Make sure any helm repo changes are reflected for the users
            helm_utils.refresh_helm_repo_information()

        except Exception as e:
            raise exception.KubeAppUploadFailure(
                name=app.name, version=app.version, reason=str(e))
        finally:
            os.chown(constants.APP_INSTALL_ROOT_PATH, orig_uid, orig_gid)

        # For system applications with plugin support, establish user override
        # entries and disable charts based on application metadata.
        db_app = self._dbapi.kube_app_get(app.name)
        app_ns = self._helm.get_helm_application_namespaces(db_app.name)
        for chart, namespaces in six.iteritems(app_ns):
            for namespace in namespaces:
                try:
                    db_chart = self._dbapi.helm_override_get(
                        db_app.id, chart, namespace)
                except exception.HelmOverrideNotFound:
                    # Create it
                    try:
                        db_chart = self._dbapi.helm_override_create(
                            {'app_id': db_app.id, 'name': chart,
                             'namespace': namespace})
                    except Exception as e:
                        LOG.exception(e)

                # Since we are uploading a fresh application. Ensure that
                # charts are disabled based on metadata
                system_overrides = db_chart.system_overrides
                system_overrides.update({common.HELM_CHART_ATTR_ENABLED:
                                         chart not in disabled_charts})

                try:
                    self._dbapi.helm_override_update(
                        db_app.id, chart, namespace, {'system_overrides':
                                                      system_overrides})
                except exception.HelmOverrideNotFound:
                    LOG.exception(e)

    def _validate_labels(self, labels):
        expr = re.compile(r'[a-z0-9]([-a-z0-9]*[a-z0-9])')
        for label in labels:
            if not expr.match(label):
                return False
        return True

    def _update_kubernetes_labels(self, hostname, label_dict):
        body = {
            'metadata': {
                'labels': {}
            }
        }
        body['metadata']['labels'].update(label_dict)
        try:
            self._kube.kube_patch_node(hostname, body)
        except exception.K8sNodeNotFound:
            pass

    def _assign_host_labels(self, hosts, labels):
        for host in hosts:
            if host.administrative != constants.ADMIN_LOCKED:
                continue
            for label_str in labels:
                k, v = label_str.split('=')
                try:
                    self._dbapi.label_create(
                        host.id, {'host_id': host.id,
                                  'label_key': k,
                                  'label_value': v})
                except exception.HostLabelAlreadyExists:
                    pass
            label_dict = {k: v for k, v in (i.split('=') for i in labels)}
            self._update_kubernetes_labels(host.hostname, label_dict)

    def _find_label(self, host_uuid, label_str):
        host_labels = self._dbapi.label_get_by_host(host_uuid)
        for label_obj in host_labels:
            if label_str == label_obj.label_key + '=' + label_obj.label_value:
                return label_obj
        return None

    def _remove_host_labels(self, hosts, labels):
        for host in hosts:
            if host.administrative != constants.ADMIN_LOCKED:
                continue
            null_labels = {}
            for label_str in labels:
                lbl_obj = self._find_label(host.uuid, label_str)
                if lbl_obj:
                    self._dbapi.label_destroy(lbl_obj.uuid)
                    key = lbl_obj.label_key
                    null_labels[key] = None
            if null_labels:
                self._update_kubernetes_labels(host.hostname, null_labels)

    def _create_storage_provisioner_secrets(self, app_name):
        """ Provide access to the system persistent storage provisioner.

        The rbd-provsioner is installed as part of system provisioning and has
        created secrets for all common default namespaces. Copy the secret to
        this application's namespace(s) to provide resolution for PVCs

        :param app_name: Name of the application
        """

        # Only set up a secret for the default storage pool (i.e. ignore
        # additional storage tiers)
        pool_secret = K8RbdProvisioner.get_user_secret_name({
            'name': constants.SB_DEFAULT_NAMES[constants.SB_TYPE_CEPH]})
        app_ns = self._helm.get_helm_application_namespaces(app_name)
        namespaces = \
            list(set([ns for ns_list in app_ns.values() for ns in ns_list]))
        for ns in namespaces:
            if (ns in [common.HELM_NS_HELM_TOOLKIT,
                       common.HELM_NS_STORAGE_PROVISIONER] or
                    self._kube.kube_get_secret(pool_secret, ns)):
                # Secret already exist
                continue

            try:
                if not self._kube.kube_get_namespace(ns):
                    self._kube.kube_create_namespace(ns)
                self._kube.kube_copy_secret(
                    pool_secret, common.HELM_NS_STORAGE_PROVISIONER, ns)
            except Exception as e:
                LOG.error(e)
                raise

    def _delete_storage_provisioner_secrets(self, app_name):
        """ Remove access to the system persistent storage provisioner.

        As part of launching a supported application, secrets were created to
        allow access to the provisioner from the application namespaces. This
        will remove those created secrets.

        :param app_name: Name of the application
        """

        # Only set up a secret for the default storage pool (i.e. ignore
        # additional storage tiers)
        pool_secret = K8RbdProvisioner.get_user_secret_name({
            'name': constants.SB_DEFAULT_NAMES[constants.SB_TYPE_CEPH]})
        app_ns = self._helm.get_helm_application_namespaces(app_name)
        namespaces = \
            list(set([ns for ns_list in app_ns.values() for ns in ns_list]))

        for ns in namespaces:
            if (ns == common.HELM_NS_HELM_TOOLKIT or
                    ns == common.HELM_NS_STORAGE_PROVISIONER):
                continue

            try:
                LOG.info("Deleting Secret %s under Namespace "
                         "%s ..." % (pool_secret, ns))
                self._kube.kube_delete_secret(
                    pool_secret, ns, grace_period_seconds=0)
                LOG.info("Secret %s under Namespace %s delete "
                         "completed." % (pool_secret, ns))
            except Exception as e:
                LOG.error(e)
                raise

    def _create_local_registry_secrets(self, app_name):
        # Temporary function to create default registry secret
        # which would be used by kubernetes to pull images from
        # local registry.
        # This should be removed after OSH supports the deployment
        # with registry has authentication turned on.
        # https://blueprints.launchpad.net/openstack-helm/+spec/
        # support-docker-registry-with-authentication-turned-on
        body = {
            'type': 'kubernetes.io/dockerconfigjson',
            'metadata': {},
            'data': {}
        }

        app_ns = self._helm.get_helm_application_namespaces(app_name)
        namespaces = \
            list(set([ns for ns_list in app_ns.values() for ns in ns_list]))
        for ns in namespaces:
            if (ns == common.HELM_NS_HELM_TOOLKIT or
                 self._kube.kube_get_secret(DOCKER_REGISTRY_SECRET, ns)):
                # Secret already exist
                continue

            try:
                local_registry_auth = get_local_docker_registry_auth()

                auth = '{0}:{1}'.format(local_registry_auth['username'],
                                        local_registry_auth['password'])
                token = '{{\"auths\": {{\"{0}\": {{\"auth\": \"{1}\"}}}}}}'.format(
                    constants.DOCKER_REGISTRY_SERVER, base64.b64encode(auth))

                body['data'].update({'.dockerconfigjson': base64.b64encode(token)})
                body['metadata'].update({'name': DOCKER_REGISTRY_SECRET,
                                         'namespace': ns})

                if not self._kube.kube_get_namespace(ns):
                    self._kube.kube_create_namespace(ns)
                self._kube.kube_create_secret(ns, body)
                LOG.info("Secret %s created under Namespace %s." % (DOCKER_REGISTRY_SECRET, ns))
            except Exception as e:
                LOG.error(e)
                raise

    def _delete_local_registry_secrets(self, app_name):
        # Temporary function to delete default registry secrets
        # which created during stx-opesntack app apply.
        # This should be removed after OSH supports the deployment
        # with registry has authentication turned on.
        # https://blueprints.launchpad.net/openstack-helm/+spec/
        # support-docker-registry-with-authentication-turned-on

        app_ns = self._helm.get_helm_application_namespaces(app_name)
        namespaces = \
            list(set([ns for ns_list in app_ns.values() for ns in ns_list]))

        for ns in namespaces:
            if ns == common.HELM_NS_HELM_TOOLKIT:
                continue

            try:
                LOG.info("Deleting Secret %s under Namespace "
                         "%s ..." % (DOCKER_REGISTRY_SECRET, ns))
                self._kube.kube_delete_secret(
                    DOCKER_REGISTRY_SECRET, ns, grace_period_seconds=0)
                LOG.info("Secret %s under Namespace %s delete "
                         "completed." % (DOCKER_REGISTRY_SECRET, ns))
            except Exception as e:
                LOG.error(e)
                raise

    def _delete_namespace(self, namespace):
        loop_timeout = 1
        timeout = 300
        try:
            LOG.info("Deleting Namespace %s ..." % namespace)
            self._kube.kube_delete_namespace(namespace,
                                             grace_periods_seconds=0)

            # Namespace termination timeout 5mins
            while(loop_timeout <= timeout):
                if not self._kube.kube_get_namespace(namespace):
                    # Namepace has been terminated
                    break
                loop_timeout += 1
                time.sleep(1)

            if loop_timeout > timeout:
                raise exception.K8sNamespaceDeleteTimeout(name=namespace)
            LOG.info("Namespace %s delete completed." % namespace)
        except Exception as e:
            LOG.error(e)
            raise

    def _delete_persistent_volume_claim(self, namespace):
        try:
            LOG.info("Deleting Persistent Volume Claim "
                     "under Namespace %s ..." % namespace)
            self._kube.kube_delete_persistent_volume_claim(namespace,
                                                           timeout_seconds=10)
            LOG.info("Persistent Volume Claim delete completed.")
        except Exception as e:
            LOG.error(e)
            raise

    def _get_list_of_charts(self, manifest_file):
        """Get the charts information from the manifest file

        The following chart data for each chart in the manifest file
        are extracted and stored into a namedtuple Chart object:
         - metadata_name
         - chart_name
         - namespace
         - location
         - release
         - pre-delete job labels

        The method returns a list of namedtuple charts which following
        the install order in the manifest chart_groups.

        :param manifest_file: the manifest file of the application
        :return: a list of namedtuple charts
        """
        charts = []
        release_prefix = ""
        chart_group = {}
        chart_groups = []
        armada_charts = {}

        with open(manifest_file, 'r') as f:
            docs = yaml.safe_load_all(f)
            for doc in docs:
                # iterative docs in the manifest file to get required
                # chart information
                try:
                    if "armada/Manifest/" in doc['schema']:
                        release_prefix = doc['data']['release_prefix']
                        chart_groups = doc['data']['chart_groups']

                    elif "armada/ChartGroup/" in doc['schema']:
                        chart_group.update(
                            {doc['metadata']['name']: {
                                'chart_group': doc['data']['chart_group'],
                                'sequenced': doc.get('data').get('sequenced', False)}})

                    elif "armada/Chart/" in doc['schema']:
                        labels = []
                        delete_resource = \
                            doc['data'].get('upgrade', {}).get('pre', {}).get('delete', [])
                        for resource in delete_resource:
                            if resource.get('type') == 'job':
                                label = ''
                                for k, v in resource['labels'].items():
                                    label = k + '=' + v + ',' + label
                                labels.append(label[:-1])

                        armada_charts.update(
                            {doc['metadata']['name']: {
                                'chart_name': doc['data']['chart_name'],
                                'namespace': doc['data']['namespace'],
                                'location': doc['data']['source']['location'],
                                'release': doc['data']['release'],
                                'labels': labels}})
                        LOG.debug("Manifest: Chart: {} Namespace: {} "
                                  "Location: {} Release: {}".format(
                                      doc['data']['chart_name'],
                                      doc['data']['namespace'],
                                      doc['data']['source']['location'],
                                      doc['data']['release']))
                except KeyError:
                    pass

            # Push Chart to the list that following the order
            # in the chart_groups(install list)
            for c_group in chart_groups:
                for chart in chart_group[c_group]['chart_group']:
                    charts.append(Chart(
                        metadata_name=chart,
                        name=armada_charts[chart]['chart_name'],
                        namespace=armada_charts[chart]['namespace'],
                        location=armada_charts[chart]['location'],
                        release=armada_charts[chart]['release'],
                        labels=armada_charts[chart]['labels'],
                        sequenced=chart_group[c_group]['sequenced']))
                    del armada_charts[chart]
                del chart_group[c_group]

            # Push Chart to the list that are not referenced
            # in the chart_groups (install list)
            if chart_group:
                for c_group in chart_group:
                    for chart in chart_group[c_group]['chart_group']:
                        charts.append(Chart(
                            metadata_name=chart,
                            name=armada_charts[chart]['chart_name'],
                            namespace=armada_charts[chart]['namespace'],
                            location=armada_charts[chart]['location'],
                            release=armada_charts[chart]['release'],
                            labels=armada_charts[chart]['labels'],
                            sequenced=chart_group[c_group]['sequenced']))
                        del armada_charts[chart]

            if armada_charts:
                for chart in armada_charts:
                    charts.append(Chart(
                        metadata_name=chart,
                        name=armada_charts[chart]['chart_name'],
                        namespace=armada_charts[chart]['namespace'],
                        location=armada_charts[chart]['location'],
                        release=armada_charts[chart]['release'],
                        labels=armada_charts[chart]['labels'],
                        sequenced=False))

        # Update each Chart in the list if there has release prefix
        # for each release
        if release_prefix:
            for i, chart in enumerate(charts):
                charts[i] = chart._replace(
                    release=release_prefix + "-" + chart.release)

        return charts

    def _get_overrides_files(self, overrides_dir, charts, app_name, mode):
        """Returns list of override files or None, used in
           application-install and application-delete."""

        missing_helm_overrides = []
        available_helm_overrides = []

        for chart in charts:
            overrides = chart.namespace + '-' + chart.name + '.yaml'
            overrides_file = os.path.join(overrides_dir, overrides)
            if not os.path.exists(overrides_file):
                missing_helm_overrides.append(overrides_file)
            else:
                available_helm_overrides.append(overrides_file)

        if missing_helm_overrides:
            LOG.error("Missing the following overrides: %s" % missing_helm_overrides)
            return None

        # Get the armada manifest overrides files
        manifest_op = self._helm.get_armada_manifest_operator(app_name)
        armada_overrides = manifest_op.load_summary(overrides_dir)

        return (available_helm_overrides, armada_overrides)

    def _generate_armada_overrides_str(self, app_name, app_version,
                                       helm_files, armada_files):
        overrides_str = ""
        if helm_files:
            overrides_str += " ".join([
                ' --values /overrides/{0}/{1}/{2}'.format(
                    app_name, app_version, os.path.basename(i))
                for i in helm_files
            ])
        if armada_files:
            overrides_str += " ".join([
                ' --values /manifests/{0}/{1}/{2}'.format(
                    app_name, app_version, os.path.basename(i))
                for i in armada_files
            ])
        return overrides_str

    def _remove_chart_overrides(self, overrides_dir, manifest_file):
        charts = self._get_list_of_charts(manifest_file)
        for chart in charts:
            if chart.name in self._helm.chart_operators:
                self._helm.remove_helm_chart_overrides(overrides_dir,
                                                       chart.name,
                                                       chart.namespace)

    def _update_app_releases_version(self, app_name):
        """Update application helm releases records

        This method retrieves the deployed helm releases and updates the
        releases records in sysinv db if needed
        :param app_name: the name of the application
        """
        try:
            deployed_releases = helm_utils.retrieve_helm_releases()

            app = self._dbapi.kube_app_get(app_name)
            app_releases = self._dbapi.kube_app_chart_release_get_all(app.id)

            for r in app_releases:
                if (r.release in deployed_releases and
                        r.namespace in deployed_releases[r.release] and
                        r.version != deployed_releases[r.release][r.namespace]):

                    self._dbapi.kube_app_chart_release_update(
                        app.id, r.release, r.namespace,
                        {'version': deployed_releases[r.release][r.namespace]})
        except Exception as e:
            LOG.exception(e)
            raise exception.SysinvException(_(
                "Failed to update/record application %s releases' versions." % str(e)))

    def _create_app_releases_version(self, app_name, app_charts):
        """Create application helm releases records

        This method creates/initializes the helm releases objects for the application.
        :param app_name: the name of the application
        :param app_charts: the charts of the application
        """
        kube_app = self._dbapi.kube_app_get(app_name)
        app_releases = self._dbapi.kube_app_chart_release_get_all(kube_app.id)
        if app_releases:
            return

        for chart in app_charts:
            values = {
                'release': chart.release,
                'version': 0,
                'namespace': chart.namespace,
                'app_id': kube_app.id
            }

            try:
                self._dbapi.kube_app_chart_release_create(values)
            except Exception as e:
                LOG.exception(e)

    def _make_armada_request_with_monitor(self, app, request, overrides_str=None):
        """Initiate armada request with monitoring

        This method delegates the armada request to docker helper and starts
        a monitoring thread to persist status and progress along the way.

        :param app: application data object
        :param request: type of request (apply or delete)
        :param overrides_str: list of overrides in string format to be applied
        """

        def _get_armada_log_stats(pattern, logfile):
            """
            TODO(tngo): In the absence of an Armada API that provides the current
            status of an apply/delete manifest operation, the progress is derived
            from specific log entries extracted from the execution logs. This
            inner method is to be replaced with an official API call when
            it becomes available.
            """
            if pattern == ROLLBACK_SEARCH_PATTERN:
                print_chart = '{print $10}'
            else:
                print_chart = '{print $NF}'
            p1 = subprocess.Popen(['docker', 'exec', ARMADA_CONTAINER_NAME,
                                   'grep', pattern, logfile],
                                   stdout=subprocess.PIPE)
            p2 = subprocess.Popen(['awk', print_chart], stdin=p1.stdout,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p1.stdout.close()
            result, err = p2.communicate()
            if result:
                # Strip out ANSI color code that might be in the text stream
                r = re.compile("\x1b\[[0-9;]*m")
                result = r.sub('', result).replace(',', '')
                matches = result.split()
                num_chart_processed = len(matches)
                last_chart_processed = matches[num_chart_processed - 1]
                if '=' in last_chart_processed:
                    last_chart_processed = last_chart_processed.split('=')[1]
                return last_chart_processed, num_chart_processed

            return None, None

        def _check_progress(monitor_flag, app, pattern, logfile):
            """ Progress monitoring task, to be run in a separate thread """
            LOG.info("Starting progress monitoring thread for app %s" % app.name)

            def _progress_adjust(app):
                # helm-toolkit doesn't count; it is not in stx-monitor
                non_helm_toolkit_apps = [constants.HELM_APP_MONITOR]
                if app.name in non_helm_toolkit_apps:
                    adjust = 0
                else:
                    adjust = 1
                return adjust

            try:
                with Timeout(INSTALLATION_TIMEOUT,
                             exception.KubeAppProgressMonitorTimeout()):
                    while True:
                        try:
                            monitor_flag.get_nowait()
                            LOG.debug("Received monitor stop signal for %s" % app.name)
                            monitor_flag.task_done()
                            break
                        except queue.Empty:
                            last, num = _get_armada_log_stats(pattern, logfile)
                            if last:
                                if app.system_app:
                                    adjust = _progress_adjust(app)
                                    percent = \
                                        round(float(num) /
                                              (len(app.charts) - adjust) * 100)
                                else:
                                    percent = round(float(num) / len(app.charts) * 100)
                                progress_str = 'processing chart: ' + last +\
                                    ', overall completion: ' + str(percent) + '%'
                                if app.progress != progress_str:
                                    LOG.info("%s" % progress_str)
                                    self._update_app_status(
                                        app, new_progress=progress_str)
                            greenthread.sleep(1)
            except Exception as e:
                # timeout or subprocess error
                LOG.exception(e)
            finally:
                LOG.info("Exiting progress monitoring thread for app %s" % app.name)

        # Body of the outer method
        mqueue = queue.Queue()
        rc = True
        logfile = ARMADA_CONTAINER_LOG_LOCATION + '/' + app.name + '-' + request + '.log'
        if request == constants.APP_APPLY_OP:
            pattern = APPLY_SEARCH_PATTERN
        elif request == constants.APP_DELETE_OP:
            pattern = DELETE_SEARCH_PATTERN
        else:
            pattern = ROLLBACK_SEARCH_PATTERN

        monitor = greenthread.spawn_after(1, _check_progress, mqueue, app,
                                          pattern, logfile)
        rc = self._docker.make_armada_request(request, app.armada_mfile,
                                              overrides_str, app.releases, logfile)
        mqueue.put('done')
        monitor.kill()
        return rc

    def _create_app_specific_resources(self, app_name):
        """Add application specific k8s resources.

        Some applications may need resources created outside of the existing
        charts to properly integrate with the current capabilities of the
        system. Create these resources here.

        :param app_name: Name of the application.
        """

        if app_name == constants.HELM_APP_OPENSTACK:
            try:
                # Copy the latest configmap with the ceph monitor information
                # required by the application into the application namespace
                if self._kube.kube_get_config_map(
                        self.APP_OPENSTACK_RESOURCE_CONFIG_MAP,
                        common.HELM_NS_OPENSTACK):

                    # Already have one. Delete it, in case it changed
                    self._kube.kube_delete_config_map(
                        self.APP_OPENSTACK_RESOURCE_CONFIG_MAP,
                        common.HELM_NS_OPENSTACK)

                # Copy the latest config map
                self._kube.kube_copy_config_map(
                    self.APP_OPENSTACK_RESOURCE_CONFIG_MAP,
                    common.HELM_NS_STORAGE_PROVISIONER,
                    common.HELM_NS_OPENSTACK)
            except Exception as e:
                LOG.error(e)
                raise

    def _delete_app_specific_resources(self, app_name):
        """Remove application specific k8s resources.

        Some applications may need resources created outside of the existing
        charts to properly integrate with the current capabilities of the
        system. Remove these resources here.

        :param app_name: Name of the application.
        """

        def _delete_ceph_persistent_volume_claim(namespace):
            self._delete_persistent_volume_claim(namespace)

            try:
                # Remove the configmap with the ceph monitor information
                # required by the application into the application namespace
                self._kube.kube_delete_config_map(
                    self.APP_OPENSTACK_RESOURCE_CONFIG_MAP,
                    namespace)
            except Exception as e:
                LOG.error(e)
                raise
            self._delete_namespace(namespace)

        if app_name == constants.HELM_APP_OPENSTACK:
            _delete_ceph_persistent_volume_claim(common.HELM_NS_OPENSTACK)
        elif app_name == constants.HELM_APP_MONITOR:
            _delete_ceph_persistent_volume_claim(common.HELM_NS_MONITOR)

    def _perform_app_recover(self, old_app, new_app, armada_process_required=True):
        """Perform application recover

        This recover method is triggered when application update failed, it cleans
        up the files/data for the new application and recover helm charts for the
        old application. If the armada process is required, armada apply is invoked
        to recover the application releases for the old version.

        The app status will be populated to "apply-failed" if recover fails so that
        the user can re-apply app.

        :param old_app: the application object that application recovering to
        :param new_app: the application object that application recovering from
        :param armada_process_required: boolean, whether armada operation is needed
        """
        LOG.info("Starting recover Application %s from version: %s to version: %s" %
                 (old_app.name, new_app.version, old_app.version))

        self._update_app_status(
            old_app, constants.APP_RECOVER_IN_PROGRESS,
            constants.APP_PROGRESS_UPDATE_ABORTED.format(old_app.version, new_app.version) +
            constants.APP_PROGRESS_RECOVER_IN_PROGRESS.format(old_app.version))
        # Set the status for the new app to inactive
        self._update_app_status(new_app, constants.APP_INACTIVE_STATE)

        try:
            self._cleanup(new_app, app_dir=False)
            self._utils._patch_report_app_dependencies(
                new_app.name + '-' + new_app.version)
            self._dbapi.kube_app_destroy(new_app.name,
                                         version=new_app.version,
                                         inactive=True)

            LOG.info("Recovering helm charts for Application %s (%s)..."
                     % (old_app.name, old_app.version))
            self._update_app_status(old_app,
                                    new_progress=constants.APP_PROGRESS_RECOVER_CHARTS)
            with self._lock:
                self._upload_helm_charts(old_app)

            rc = True
            if armada_process_required:
                overrides_str = ''
                old_app.charts = self._get_list_of_charts(old_app.armada_mfile_abs)
                if old_app.system_app:
                    (helm_files, armada_files) = self._get_overrides_files(
                        old_app.overrides_dir, old_app.charts, old_app.name, mode=None)

                    overrides_str = self._generate_armada_overrides_str(
                        old_app.name, old_app.version, helm_files, armada_files)

                if self._make_armada_request_with_monitor(old_app,
                                                          constants.APP_APPLY_OP,
                                                          overrides_str):
                    old_app_charts = [c.release for c in old_app.charts]
                    deployed_releases = helm_utils.retrieve_helm_releases()
                    for new_chart in new_app.charts:
                        if (new_chart.release not in old_app_charts and
                                new_chart.release in deployed_releases):
                            # Cleanup the releases in the new application version
                            # but are not in the old application version
                            helm_utils.delete_helm_release(new_chart.release)
                else:
                    rc = False

        except Exception as e:
            # ie. patch report error, cleanup application files error
            #     helm release delete failure
            self._update_app_status(
                old_app, constants.APP_APPLY_SUCCESS,
                constants.APP_PROGRESS_UPDATE_ABORTED.format(old_app.version, new_app.version) +
                constants.APP_PROGRESS_RECOVER_COMPLETED.format(old_app.version) +
                constants.APP_PROGRESS_CLEANUP_FAILED.format(new_app.version) +
                'Please check logs for details.')
            LOG.error(e)
            return

        if rc:
            self._update_app_status(
                old_app, constants.APP_APPLY_SUCCESS,
                constants.APP_PROGRESS_UPDATE_ABORTED.format(old_app.version, new_app.version) +
                constants.APP_PROGRESS_RECOVER_COMPLETED.format(old_app.version) +
                'Please check logs for details.')
            # Recovery from an app update failure succeeded, clear app alarm
            self._clear_app_alarm(old_app.name)
            LOG.info("Application %s recover to version %s completed."
                     % (old_app.name, old_app.version))
        else:
            self._update_app_status(
                old_app, constants.APP_APPLY_FAILURE,
                constants.APP_PROGRESS_UPDATE_ABORTED.format(old_app.version, new_app.version) +
                constants.APP_PROGRESS_RECOVER_ABORTED.format(old_app.version) +
                'Please check logs for details.')
            LOG.error("Application %s recover to version %s aborted!"
                      % (old_app.name, old_app.version))

    def _perform_app_rollback(self, from_app, to_app):
        """Perform application rollback request

        This method invokes Armada to rollback the application releases to
        previous installed versions. The jobs for the current installed
        releases require to be cleaned up before starting armada rollback.

        :param from_app: application object that application updating from
        :param to_app: application object that application updating to
        :return boolean: whether application rollback was successful
        """

        LOG.info("Application %s (%s) rollback started." % (to_app.name, to_app.version))

        try:
            if AppOperator.is_app_aborted(to_app.name):
                raise exception.KubeAppAbort()

            to_db_app = self._dbapi.kube_app_get(to_app.name)
            to_app_releases = \
                self._dbapi.kube_app_chart_release_get_all(to_db_app.id)

            from_db_app = self._dbapi.kube_app_get_inactive_by_name_version(
                from_app.name, version=from_app.version)
            from_app_releases = \
                self._dbapi.kube_app_chart_release_get_all(from_db_app.id)
            from_app_r_dict = {r.release: r.version for r in from_app_releases}

            self._update_app_status(
                to_app, new_progress=constants.APP_PROGRESS_ROLLBACK_RELEASES)

            if AppOperator.is_app_aborted(to_app.name):
                raise exception.KubeAppAbort()

            charts_sequence = {c.release: c.sequenced for c in to_app.charts}
            charts_labels = {c.release: c.labels for c in to_app.charts}
            for to_app_r in to_app_releases:
                if to_app_r.version != 0:
                    if (to_app_r.release not in from_app_r_dict or
                            (to_app_r.release in from_app_r_dict and
                             to_app_r.version != from_app_r_dict[to_app_r.release])):
                        # Append the release which needs to be rolled back
                        to_app.releases.append(
                            {'release': to_app_r.release,
                             'version': to_app_r.version,
                             'sequenced': charts_sequence[to_app_r.release]})

                        # Cleanup the jobs for the current installed release
                        if to_app_r.release in charts_labels:
                            for label in charts_labels[to_app_r.release]:
                                self._kube.kube_delete_collection_namespaced_job(
                                    to_app_r.namespace, label)
                        LOG.info("Jobs deleted for release %s" % to_app_r.release)

            if AppOperator.is_app_aborted(to_app.name):
                raise exception.KubeAppAbort()

            if self._make_armada_request_with_monitor(to_app,
                                                      constants.APP_ROLLBACK_OP):
                self._update_app_status(to_app, constants.APP_APPLY_SUCCESS,
                                        constants.APP_PROGRESS_COMPLETED)
                LOG.info("Application %s (%s) rollback completed."
                         % (to_app.name, to_app.version))
                return True
        except exception.KubeAppAbort:
            # If the update operation is aborted before Armada request is made,
            # we don't want to return False which would trigger the recovery
            # routine with an Armada request.
            raise
        except Exception as e:
            # unexpected KubeAppNotFound, KubeAppInactiveNotFound, KeyError
            # k8s exception:fail to cleanup release jobs
            LOG.exception(e)

        LOG.error("Application rollback aborted!")
        return False

    def _is_system_app(self, name):
        if name in self._helm.get_helm_applications():
            return True
        return False

    def perform_app_upload(self, rpc_app, tarfile):
        """Process application upload request

        This method validates the application manifest. If Helm charts are
        included, they are validated and uploaded to local Helm repo. It also
        downloads the required docker images for custom apps during upload
        stage.

        :param rpc_app: application object in the RPC request
        :param tarfile: location of application tarfile
        """

        app = AppOperator.Application(rpc_app,
                                      self._is_system_app(rpc_app.get('name')))
        LOG.info("Application %s (%s) upload started." % (app.name, app.version))

        try:
            if not self._helm.version_check(app.name, app.version):
                LOG.info("Application %s (%s) upload rejected. Unsupported version."
                         % (app.name, app.version))
                raise exception.KubeAppUploadFailure(
                    name=app.name,
                    version=app.version,
                    reason="Unsupported application version.")

            app.tarfile = tarfile

            if cutils.is_url(app.tarfile):
                self._update_app_status(
                    app, new_progress=constants.APP_PROGRESS_TARFILE_DOWNLOAD)

                downloaded_tarfile = self._download_tarfile(app)

                if downloaded_tarfile is None:
                    raise exception.KubeAppUploadFailure(
                        name=app.name,
                        version=app.version,
                        reason="Failed to find the downloaded tarball.")
                else:
                    app.tarfile = downloaded_tarfile

                app.downloaded_tarfile = True

            # Full extraction of application tarball at /scratch/apps.
            # Manifest file is placed under /opt/platform/armada
            # which is managed by drbd-sync and visible to Armada.
            self._update_app_status(
                app, new_progress=constants.APP_PROGRESS_EXTRACT_TARFILE)

            with self._lock:
                self._extract_tarfile(app)
            shutil.copy(app.mfile_abs, app.armada_mfile_abs)

            if not self._docker.make_armada_request(
                    'validate', manifest_file=app.armada_mfile):
                raise exception.KubeAppUploadFailure(
                    name=app.name,
                    version=app.version,
                    reason="Failed to validate application manifest.")

            self._update_app_status(
                app, new_progress=constants.APP_PROGRESS_VALIDATE_UPLOAD_CHARTS)

            if os.path.isdir(app.charts_dir):
                self._validate_helm_charts(app)
                with self._lock:
                    self._upload_helm_charts(app)

            self._save_images_list(app)
            if app.patch_dependencies:
                self._utils._patch_report_app_dependencies(
                    app.name + '-' + app.version, app.patch_dependencies)
            self._create_app_releases_version(app.name, app.charts)
            self._update_app_status(app, constants.APP_UPLOAD_SUCCESS,
                                    constants.APP_PROGRESS_COMPLETED)
            LOG.info("Application %s (%s) upload completed." % (app.name, app.version))
            return app
        except exception.KubeAppUploadFailure as e:
            LOG.exception(e)
            self._abort_operation(app, constants.APP_UPLOAD_OP, str(e))
            raise
        except Exception as e:
            LOG.exception(e)
            self._abort_operation(app, constants.APP_UPLOAD_OP)
            raise exception.KubeAppUploadFailure(
                name=app.name, version=app.version, reason=e)

    def perform_app_apply(self, rpc_app, mode, caller=None):
        """Process application install request

        This method processes node labels per configuration and invokes
        Armada to apply the application manifest.

        For OpenStack app (system app), the method generates combined
        overrides (a merge between system and user overrides if available)
        for the charts that comprise the app before downloading docker images
        and applying the manifest.

        Usage: the method can be invoked at initial install or after the
               user has either made some manual configuration changes or
               or applied (new) user overrides to some Helm chart(s) to
               correct/update a previous manifest apply.

        :param rpc_app: application object in the RPC request
        :param mode: mode to control how to apply application manifest
        :param caller: internal caller, None if it is an RPC call,
                       otherwise apply is invoked from update method
        :return boolean: whether application apply was successful
        """

        app = AppOperator.Application(rpc_app,
                                      self._is_system_app(rpc_app.get('name')))

        # If apply is called from update method, the app's abort status has
        # already been registered.
        if not caller:
            self._register_app_abort(app.name)
            self._raise_app_alarm(app.name, constants.APP_APPLY_IN_PROGRESS,
                                  fm_constants.FM_ALARM_ID_APPLICATION_APPLYING,
                                  fm_constants.FM_ALARM_SEVERITY_WARNING,
                                  _("Application Apply In Progress"),
                                  fm_constants.FM_ALARM_TYPE_0,
                                  _("No action required."),
                                  True)

        # Remove the pending auto re-apply if it is being triggered manually
        if (app.name == constants.HELM_APP_OPENSTACK and
                os.path.isfile(constants.APP_OPENSTACK_PENDING_REAPPLY_FLAG)):
            # Consume the reapply flag
            os.remove(constants.APP_OPENSTACK_PENDING_REAPPLY_FLAG)

            # Clear the pending automatic reapply alarm
            app_alarms = self._fm_api.get_faults_by_id(
                fm_constants.FM_ALARM_ID_APPLICATION_REAPPLY_PENDING)
            if app_alarms:
                self._fm_api.clear_fault(app_alarms[0].alarm_id,
                                         app_alarms[0].entity_instance_id)

        LOG.info("Application %s (%s) apply started." % (app.name, app.version))

        overrides_str = ''
        ready = True
        try:
            app.charts = self._get_list_of_charts(app.armada_mfile_abs)
            if app.system_app:
                if AppOperator.is_app_aborted(app.name):
                    raise exception.KubeAppAbort()

                self._create_local_registry_secrets(app.name)
                self._create_storage_provisioner_secrets(app.name)
                self._create_app_specific_resources(app.name)

            self._update_app_status(
                app, new_progress=constants.APP_PROGRESS_GENERATE_OVERRIDES)

            if AppOperator.is_app_aborted(app.name):
                raise exception.KubeAppAbort()

            LOG.info("Generating application overrides...")
            self._helm.generate_helm_application_overrides(
                app.overrides_dir, app.name, mode, cnamespace=None,
                armada_format=True, armada_chart_info=app.charts, combined=True)
            (helm_files, armada_files) = self._get_overrides_files(
                app.overrides_dir, app.charts, app.name, mode)

            if helm_files or armada_files:
                LOG.info("Application overrides generated.")
                overrides_str = self._generate_armada_overrides_str(
                    app.name, app.version, helm_files, armada_files)

                self._update_app_status(
                    app, new_progress=constants.APP_PROGRESS_DOWNLOAD_IMAGES)

                if AppOperator.is_app_aborted(app.name):
                    raise exception.KubeAppAbort()

                self._download_images(app)
            else:
                ready = False
        except Exception as e:
            LOG.exception(e)
            if AppOperator.is_app_aborted(app.name):
                self._abort_operation(app, constants.APP_APPLY_OP,
                                      user_initiated=True)
            else:
                self._abort_operation(app, constants.APP_APPLY_OP, str(e))

            if not caller:
                # If apply is not called from update method, deregister the app's
                # abort status. Otherwise, it will be done in the update method.
                self._deregister_app_abort(app.name)

            if isinstance(e, exception.KubeAppApplyFailure):
                # ex:Image download failure
                raise
            else:
                # ex:K8s resource creation failure, user abort
                raise exception.KubeAppApplyFailure(
                    name=app.name, version=app.version, reason=e)

        try:
            if ready:
                if app.name == constants.HELM_APP_OPENSTACK:
                    # For stx-openstack app, if the apply operation was terminated
                    # (e.g. user aborted, controller swacted, sysinv conductor
                    # restarted) while compute-kit charts group was being deployed,
                    # Tiller may still be processing these charts. Issuing another
                    # manifest apply request while there are pending install of libvirt,
                    # neutron and/or nova charts will result in reapply failure.
                    #
                    # Wait up to 10 minutes for Tiller to finish its transaction
                    # from previous apply before making a new manifest apply request.
                    LOG.info("Wait if there are openstack charts in pending install...")
                    for i in range(CHARTS_PENDING_INSTALL_ITERATIONS):
                        result = helm_utils.get_openstack_pending_install_charts()
                        if not result:
                            break

                        if AppOperator.is_app_aborted(app.name):
                            raise exception.KubeAppAbort()
                        greenthread.sleep(10)
                    if result:
                        self._abort_operation(app, constants.APP_APPLY_OP)
                        raise exception.KubeAppApplyFailure(
                            name=app.name, version=app.version,
                            reason="Timed out while waiting for some charts that "
                                   "are still in pending install in previous application "
                                   "apply to clear. Please try again later.")

                self._update_app_status(
                    app, new_progress=constants.APP_PROGRESS_APPLY_MANIFEST)

                if AppOperator.is_app_aborted(app.name):
                    raise exception.KubeAppAbort()
                if self._make_armada_request_with_monitor(app,
                                                          constants.APP_APPLY_OP,
                                                          overrides_str):
                    self._update_app_releases_version(app.name)
                    self._update_app_status(app,
                                            constants.APP_APPLY_SUCCESS,
                                            constants.APP_PROGRESS_COMPLETED)
                    app.update_active(True)
                    if not caller:
                        self._clear_app_alarm(app.name)
                    LOG.info("Application %s (%s) apply completed." % (app.name, app.version))
                    return True
        except Exception as e:
            # ex: update release version failure, user abort
            LOG.exception(e)

        # If it gets here, something went wrong
        if AppOperator.is_app_aborted(app.name):
            self._abort_operation(app, constants.APP_APPLY_OP, user_initiated=True)
        else:
            self._abort_operation(app, constants.APP_APPLY_OP)

        if not caller:
            # If apply is not called from update method, deregister the app's abort status.
            # Otherwise, it will be done in the update method.
            self._deregister_app_abort(app.name)

        return False

    def perform_app_update(self, from_rpc_app, to_rpc_app, tarfile, operation):
        """Process application update request

        This method leverages the existing application upload workflow to
        validate/upload the new application tarfile, then invokes Armada
        apply or rollback to update application from an applied version
        to the new version. If any failure happens during updating, the
        recover action will be triggered to recover the application to
        the old version.

        After apply/rollback to the new version is done, the files for the
        old application version will be cleaned up as well as the releases
        which are not in the new application version.

        The app status will be populated to "applied" once update is completed
        so that user can continue applying app with user overrides.

        Usage ex: the method can be used to update from v1 to v2 and also
                  update back from v2 to v1

        :param from_rpc_app: application object in the RPC request that
                             application updating from
        :param to_rpc_app: application object in the RPC request that
                           application updating to
        :param tarfile: location of application tarfile
        :param operation: apply or rollback
        """

        from_app = AppOperator.Application(from_rpc_app,
            from_rpc_app.get('name') in self._helm.get_helm_applications())
        to_app = AppOperator.Application(to_rpc_app,
            to_rpc_app.get('name') in self._helm.get_helm_applications())

        self._register_app_abort(to_app.name)
        self._raise_app_alarm(to_app.name, constants.APP_UPDATE_IN_PROGRESS,
                              fm_constants.FM_ALARM_ID_APPLICATION_UPDATING,
                              fm_constants.FM_ALARM_SEVERITY_WARNING,
                              _("Application Update In Progress"),
                              fm_constants.FM_ALARM_TYPE_0,
                              _("No action required."),
                              True)
        LOG.info("Start updating Application %s from version %s to version %s ..."
                 % (to_app.name, from_app.version, to_app.version))

        try:
            # Upload new app tarball
            to_app = self.perform_app_upload(to_rpc_app, tarfile)

            self._update_app_status(to_app, constants.APP_UPDATE_IN_PROGRESS)

            result = False
            if operation == constants.APP_APPLY_OP:
                result = self.perform_app_apply(to_rpc_app, mode=None, caller='update')
            elif operation == constants.APP_ROLLBACK_OP:
                result = self._perform_app_rollback(from_app, to_app)

            if not result:
                LOG.error("Application %s update from version %s to version "
                          "%s aborted." % (to_app.name, from_app.version, to_app.version))
                return self._perform_app_recover(from_app, to_app)

            self._update_app_status(to_app, constants.APP_UPDATE_IN_PROGRESS,
                                    "cleanup application version {}".format(from_app.version))

            # App apply/rollback succeeded
            # Starting cleanup old application
            from_app.charts = self._get_list_of_charts(from_app.armada_mfile_abs)
            to_app_charts = [c.release for c in to_app.charts]
            deployed_releases = helm_utils.retrieve_helm_releases()
            for from_chart in from_app.charts:
                if (from_chart.release not in to_app_charts and
                        from_chart.release in deployed_releases):
                    # Cleanup the releases in the old application version
                    # but are not in the new application version
                    helm_utils.delete_helm_release(from_chart.release)
                    LOG.info("Helm release %s for Application %s (%s) deleted"
                             % (from_chart.release, from_app.name, from_app.version))

            self._cleanup(from_app, app_dir=False)
            self._utils._patch_report_app_dependencies(
                from_app.name + '-' + from_app.version)

            self._update_app_status(
                to_app, constants.APP_APPLY_SUCCESS,
                constants.APP_PROGRESS_UPDATE_COMPLETED.format(from_app.version,
                                                               to_app.version))
            LOG.info("Application %s update from version %s to version "
                     "%s completed." % (to_app.name, from_app.version, to_app.version))
        except (exception.KubeAppUploadFailure,
                exception.KubeAppApplyFailure,
                exception.KubeAppAbort):
            # Error occurs during app uploading or applying but before
            # armada apply process...
            # ie.images download/k8s resource creation failure
            # Start recovering without trigger armada process
            return self._perform_app_recover(from_app, to_app,
                                             armada_process_required=False)
        except Exception as e:
            # Application update successfully(armada apply/rollback)
            # Error occurs during cleanup old app
            # ie. delete app files failure, patch controller failure,
            #     helm release delete failure
            self._update_app_status(
                to_app, constants.APP_APPLY_SUCCESS,
                constants.APP_PROGRESS_UPDATE_COMPLETED.format(from_app.version, to_app.version) +
                constants.APP_PROGRESS_CLEANUP_FAILED.format(from_app.version) +
                'please check logs for detail.')
            LOG.exception(e)
        finally:
            self._deregister_app_abort(to_app.name)

        self._clear_app_alarm(to_app.name)
        return True

    def perform_app_remove(self, rpc_app):
        """Process application remove request

        This method invokes Armada to delete the application manifest.
        For system app, it also cleans up old test pods.

        :param rpc_app: application object in the RPC request
        :return boolean: whether application remove was successful
        """

        app = AppOperator.Application(rpc_app,
            rpc_app.get('name') in self._helm.get_helm_applications())
        self._register_app_abort(app.name)
        LOG.info("Application (%s) remove started." % app.name)
        rc = True

        app.charts = self._get_list_of_charts(app.armada_mfile_abs)
        app.update_active(False)
        self._update_app_status(
            app, new_progress=constants.APP_PROGRESS_DELETE_MANIFEST)

        if self._make_armada_request_with_monitor(app, constants.APP_DELETE_OP):
            # After armada delete, the data for the releases are purged from
            # tiller/etcd, the releases info for the active app stored in sysinv
            # db should be set back to 0 and the inactive apps require to be
            # destroyed too.
            db_app = self._dbapi.kube_app_get(app.name)
            app_releases = self._dbapi.kube_app_chart_release_get_all(db_app.id)
            for r in app_releases:
                if r.version != 0:
                    self._dbapi.kube_app_chart_release_update(
                        db_app.id, r.release, r.namespace, {'version': 0})
            if self._dbapi.kube_app_get_inactive(app.name):
                self._dbapi.kube_app_destroy(app.name, inactive=True)

            if app.system_app:

                try:
                    self._delete_local_registry_secrets(app.name)
                    self._delete_storage_provisioner_secrets(app.name)
                    self._delete_app_specific_resources(app.name)
                except Exception as e:
                    self._abort_operation(app, constants.APP_REMOVE_OP)
                    LOG.exception(e)
                    self._deregister_app_abort(app.name)
                    return False

            self._update_app_status(app, constants.APP_UPLOAD_SUCCESS,
                                    constants.APP_PROGRESS_COMPLETED)
            # In case there is an existing alarm for previous remove failure
            self._clear_app_alarm(app.name)
            LOG.info("Application (%s) remove completed." % app.name)
        else:
            if AppOperator.is_app_aborted(app.name):
                self._abort_operation(app, constants.APP_REMOVE_OP,
                                      user_initiated=True)
            else:
                self._abort_operation(app, constants.APP_REMOVE_OP)
            rc = False

        self._deregister_app_abort(app.name)
        return rc

    def activate(self, rpc_app):
        app = AppOperator.Application(
            rpc_app,
            rpc_app.get('name') in self._helm.get_helm_applications())
        with self._lock:
            return app.update_active(True)

    def deactivate(self, rpc_app):
        app = AppOperator.Application(
            rpc_app,
            rpc_app.get('name') in self._helm.get_helm_applications())
        with self._lock:
            return app.update_active(False)

    def get_appname(self, rpc_app):
        app = AppOperator.Application(
            rpc_app,
            rpc_app.get('name') in self._helm.get_helm_applications())
        return app.name

    def is_app_active(self, rpc_app):
        app = AppOperator.Application(
            rpc_app,
            rpc_app.get('name') in self._helm.get_helm_applications())
        return app.active

    def perform_app_abort(self, rpc_app):
        """Process application abort request

        This method retrieves the latest application status from the
        database and sets the abort flag if the apply/update/remove
        operation is still in progress. The corresponding app processing
        thread will check the flag and abort the operation in the very
        next opportunity. The method also stops the Armada service and
        clears locks in case the app processing thread has made a
        request to Armada.

        :param rpc_app: application object in the RPC request
        """

        app = AppOperator.Application(rpc_app,
            rpc_app.get('name') in self._helm.get_helm_applications())

        # Retrieve the latest app status from the database
        db_app = self._dbapi.kube_app_get(app.name)
        if db_app.status in [constants.APP_APPLY_IN_PROGRESS,
                             constants.APP_UPDATE_IN_PROGRESS,
                             constants.APP_REMOVE_IN_PROGRESS]:
            # Turn on the abort flag so the processing thread that is
            # in progress can bail out in the next opportunity.
            self._set_abort_flag(app.name)

            # Stop the Armada request in case it has reached this far and
            # remove locks.
            with self._lock:
                self._docker.stop_armada_request()
                self._clear_armada_locks()
        else:
            # Either the previous operation has completed or already failed
            LOG.info("Abort request ignored. The previous operation for app %s "
                     "has either completed or failed." % app.name)

    def perform_app_delete(self, rpc_app):
        """Process application remove request

        This method removes the application entry from the database and
        performs cleanup which entails removing node labels where applicable
        and purge all application files from the system.

        :param rpc_app: application object in the RPC request
        """

        app = AppOperator.Application(rpc_app,
            rpc_app.get('name') in self._helm.get_helm_applications())
        try:
            self._dbapi.kube_app_destroy(app.name)
            self._cleanup(app)
            self._utils._patch_report_app_dependencies(app.name + '-' + app.version)
            # One last check of app alarm, should be no-op unless the
            # user deletes the application following an upload failure.
            self._clear_app_alarm(app.name)
            LOG.info("Application (%s) has been purged from the system." %
                     app.name)
            msg = None
        except Exception as e:
            # Possible exceptions are KubeAppDeleteFailure,
            # OSError and unexpectedly KubeAppNotFound
            LOG.exception(e)
            msg = str(e)
        return msg

    class Application(object):
        """ Data object to encapsulate all data required to
            support application related operations.
        """

        def __init__(self, rpc_app, is_system_app):
            self._kube_app = rpc_app
            self.path = os.path.join(constants.APP_INSTALL_PATH,
                                     self._kube_app.get('name'),
                                     self._kube_app.get('app_version'))
            self.charts_dir = os.path.join(self.path, 'charts')
            self.images_dir = os.path.join(self.path, 'images')
            self.tarfile = None
            self.downloaded_tarfile = False
            self.system_app = is_system_app
            self.overrides_dir = generate_overrides_dir(
                self._kube_app.get('name'),
                self._kube_app.get('app_version'))
            self.armada_mfile_dir = cutils.generate_armada_manifest_dir(
                self._kube_app.get('name'),
                self._kube_app.get('app_version'))
            self.armada_mfile = generate_armada_manifest_filename(
                self._kube_app.get('name'),
                self._kube_app.get('app_version'),
                self._kube_app.get('manifest_file'))
            self.armada_mfile_abs = cutils.generate_armada_manifest_filename_abs(
                self.armada_mfile_dir,
                self._kube_app.get('name'),
                self._kube_app.get('manifest_file'))
            self.mfile_abs = generate_manifest_filename_abs(
                self._kube_app.get('name'),
                self._kube_app.get('app_version'),
                self._kube_app.get('manifest_file'))
            self.imgfile_abs = generate_images_filename_abs(
                self.armada_mfile_dir,
                self._kube_app.get('name'))

            self.patch_dependencies = []
            self.charts = []
            self.releases = []

        @property
        def name(self):
            return self._kube_app.get('name')

        @property
        def version(self):
            return self._kube_app.get('app_version')

        @property
        def status(self):
            return self._kube_app.get('status')

        @property
        def progress(self):
            return self._kube_app.get('progress')

        @property
        def active(self):
            return self._kube_app.get('active')

        @property
        def recovery_attempts(self):
            return self._kube_app.get('recovery_attempts')

        def update_status(self, new_status, new_progress):
            self._kube_app.status = new_status
            if new_progress:
                self._kube_app.progress = new_progress
            self._kube_app.save()

        def update_active(self, active):
            was_active = self.active
            if active != self.active:
                self._kube_app.active = active
                self._kube_app.save()
            return was_active

        def regenerate_manifest_filename(self, new_mname, new_mfile):
            self._kube_app.manifest_name = new_mname
            self._kube_app.manifest_file = new_mfile
            self.armada_mfile = generate_armada_manifest_filename(
                self.name, self.version, new_mfile)
            self.armada_mfile_abs = cutils.generate_armada_manifest_filename_abs(
                self.armada_mfile_dir, self.name, new_mfile)
            self.mfile_abs = generate_manifest_filename_abs(
                self.name, self.version, new_mfile)

        def regenerate_application_info(self, new_name, new_version, new_patch_dependencies):
            self._kube_app.name = new_name
            self._kube_app.app_version = new_version
            self.system_app = \
                (self.name == constants.HELM_APP_OPENSTACK or
                 self.name == constants.HELM_APP_MONITOR)

            new_armada_dir = cutils.generate_armada_manifest_dir(
                self.name, self.version)
            shutil.move(self.armada_mfile_dir, new_armada_dir)
            shutil.rmtree(os.path.dirname(self.armada_mfile_dir))
            self.armada_mfile_dir = new_armada_dir

            new_path = os.path.join(
                constants.APP_INSTALL_PATH, self.name, self.version)
            shutil.move(self.path, new_path)
            shutil.rmtree(os.path.dirname(self.path))
            self.path = new_path

            self.charts_dir = os.path.join(self.path, 'charts')
            self.images_dir = os.path.join(self.path, 'images')
            self.imgfile_abs = \
                generate_images_filename_abs(self.armada_mfile_dir, self.name)
            self.overrides_dir = generate_overrides_dir(self.name, self.version)
            self.patch_dependencies = new_patch_dependencies


class DockerHelper(object):
    """ Utility class to encapsulate Docker related operations """

    def __init__(self, dbapi):
        self._dbapi = dbapi
        self._lock = threading.Lock()
        self.registries_info = \
            copy.deepcopy(constants.DEFAULT_REGISTRIES_INFO)

    def _parse_barbican_secret(self, secret_ref):
        """Get the registry credentials from the
           barbican secret payload

           The format of the credentials stored in
           barbican secret:
           username:xxx password:xxx

        :param secret_ref: barbican secret ref/uuid
        :return: dict of registry credentials
        """
        operator = openstack.OpenStackOperator(self._dbapi)
        payload = operator.get_barbican_secret_payload(secret_ref)
        if not payload:
            raise exception.SysinvException(_(
                "Unable to get the payload of Barbican secret "
                "%s" % secret_ref))

        try:
            username, password = payload.split()
            username = username.split('username:')[1]
            password = password.split('password:')[1]
            return dict(username=username, password=password)
        except Exception as e:
            LOG.error("Unable to parse the secret payload, "
                      "unknown format of the registry secret: %s" % e)
            raise exception.SysinvException(_(
                "Unable to parse the secret payload"))

    def _retrieve_specified_registries(self):
        registries = self._dbapi.service_parameter_get_all(
            service=constants.SERVICE_TYPE_DOCKER,
            name=constants.SERVICE_PARAM_NAME_DOCKER_URL)

        if not registries:
            # return directly if no user specified registries
            return

        registries_auth_db = self._dbapi.service_parameter_get_all(
            service=constants.SERVICE_TYPE_DOCKER,
            name=constants.SERVICE_PARAM_NAME_DOCKER_AUTH_SECRET)
        registries_auth = {r.section: r.value for r in registries_auth_db}

        for r in registries:
            try:
                self.registries_info[r.section]['registry_replaced'] = str(r.value)
                if r.section in registries_auth:
                    secret_ref = str(registries_auth[r.section])
                    if secret_ref != 'None':
                        # If user specified registry requires the
                        # authentication, get the registry auth
                        # from barbican secret
                        auth = self._parse_barbican_secret(secret_ref)
                        self.registries_info[r.section]['registry_auth'] = auth
            except exception.SysinvException:
                raise exception.SysinvException(_(
                    "Unable to get the credentials to access "
                    "registry %s" % str(r.value)))
            except KeyError:
                # Unexpected
                pass

    def _reset_registries_info(self):
        # Set cached registries information
        # back to default
        if self.registries_info != \
                constants.DEFAULT_REGISTRIES_INFO:
            self.registries_info = copy.deepcopy(
                constants.DEFAULT_REGISTRIES_INFO)

    def _get_img_tag_with_registry(self, pub_img_tag):
        """Regenerate public image tag with user specified registries
        """

        if self.registries_info == constants.DEFAULT_REGISTRIES_INFO:
            # return if no user specified registries
            return pub_img_tag, None

        # An example of passed public image tag:
        # docker.io/starlingx/stx-keystone:latest
        # extracted registry_name = docker.io
        # extracted img_name = starlingx/stx-keystone:latest
        registry_name = pub_img_tag[0:1 + pub_img_tag.find('/')].replace('/', '')
        img_name = pub_img_tag[1 + pub_img_tag.find('/'):]

        for registry_info in self.registries_info.values():
            if registry_name == registry_info['registry_default']:
                registry = registry_info['registry_replaced']
                registry_auth = registry_info['registry_auth']

                if registry:
                    return registry + '/' + img_name, registry_auth
                return pub_img_tag, registry_auth

        # If extracted registry_name is none of k8s.gcr.io, gcr.io,
        # quay.io and docker.io or no registry_name specified in image
        # tag, use user specified docker registry as default
        registry = self.registries_info[
            constants.SERVICE_PARAM_SECTION_DOCKER_DOCKER_REGISTRY]['registry_replaced']
        registry_auth = self.registries_info[
            constants.SERVICE_PARAM_SECTION_DOCKER_DOCKER_REGISTRY]['registry_auth']

        if registry:
            LOG.info("Registry %s not recognized or docker.io repository "
                     "detected. Pulling from public/private registry"
                     % registry_name)
            return registry + '/' + pub_img_tag, registry_auth
        return pub_img_tag, registry_auth

    def _start_armada_service(self, client):
        try:
            container = client.containers.get(ARMADA_CONTAINER_NAME)
            if container.status != 'running':
                LOG.info("Restarting Armada service...")
                container.restart()
            return container
        except Exception:
            LOG.info("Starting Armada service...")
            try:
                # Create the armada log folder if it does not exists
                if not os.path.exists(ARMADA_HOST_LOG_LOCATION):
                    os.mkdir(ARMADA_HOST_LOG_LOCATION)
                    os.chmod(ARMADA_HOST_LOG_LOCATION, 0o755)
                    os.chown(ARMADA_HOST_LOG_LOCATION, 1000, grp.getgrnam("sys_protected").gr_gid)

                # First make kubernetes config accessible to Armada. This
                # is a work around the permission issue in Armada container.
                kube_config = os.path.join(constants.APP_SYNCED_DATA_PATH,
                                           'admin.conf')
                shutil.copy('/etc/kubernetes/admin.conf', kube_config)
                os.chown(kube_config, 1000, grp.getgrnam("sys_protected").gr_gid)

                overrides_dir = common.HELM_OVERRIDES_PATH
                manifests_dir = constants.APP_SYNCED_DATA_PATH
                logs_dir = ARMADA_HOST_LOG_LOCATION
                LOG.info("kube_config=%s, manifests_dir=%s, "
                         "overrides_dir=%s, logs_dir=%s." %
                         (kube_config, manifests_dir, overrides_dir, logs_dir))

                binds = {
                    kube_config: {'bind': '/armada/.kube/config', 'mode': 'ro'},
                    manifests_dir: {'bind': '/manifests', 'mode': 'ro'},
                    overrides_dir: {'bind': '/overrides', 'mode': 'ro'},
                    logs_dir: {'bind': ARMADA_CONTAINER_LOG_LOCATION, 'mode': 'rw'}}

                armada_image = client.images.list(CONF.armada_image_tag)
                # Pull Armada image if it's not available
                if not armada_image:
                    LOG.info("Downloading Armada image %s ..." % CONF.armada_image_tag)

                    quay_registry_secret = self._dbapi.service_parameter_get_all(
                        service=constants.SERVICE_TYPE_DOCKER,
                        section=constants.SERVICE_PARAM_SECTION_DOCKER_QUAY_REGISTRY,
                        name=constants.SERVICE_PARAM_NAME_DOCKER_AUTH_SECRET)
                    if quay_registry_secret:
                        quay_registry_auth = self._parse_barbican_secret(
                            quay_registry_secret[0].value)
                    else:
                        quay_registry_auth = None

                    client.images.pull(CONF.armada_image_tag,
                                       auth_config=quay_registry_auth)
                    LOG.info("Armada image %s downloaded!" % CONF.armada_image_tag)

                container = client.containers.run(
                    CONF.armada_image_tag,
                    name=ARMADA_CONTAINER_NAME,
                    detach=True,
                    volumes=binds,
                    restart_policy={'Name': 'always'},
                    network_mode='host',
                    command=None)
                LOG.info("Armada service started!")
                return container
            except OSError as oe:
                LOG.error("Unable to make kubernetes config accessible to "
                          "armada: %s" % oe)
            except Exception as e:
                # Possible docker exceptions are: RuntimeError, ContainerError,
                # ImageNotFound and APIError
                LOG.error("Docker error while launching Armada container: %s", e)
                os.unlink(kube_config)
            return None

    def make_armada_request(self, request, manifest_file='', overrides_str='',
                            app_releases=None, logfile=None):

        if logfile is None:
            logfile = request + '.log'

        if app_releases is None:
            app_releases = []

        rc = True

        # Instruct Armada to use the tiller service since it does not properly
        # process IPv6 endpoints, therefore use a resolvable hostname
        tiller_host = " --tiller-host tiller-deploy.kube-system.svc.cluster.local"

        try:
            client = docker.from_env(timeout=INSTALLATION_TIMEOUT)

            # It causes problem if multiple threads attempt to start the
            # same container, so add lock to ensure only one thread can
            # start the Armada container at a time
            with self._lock:
                armada_svc = self._start_armada_service(client)

            if armada_svc:
                if request == 'validate':
                    cmd = 'armada validate ' + manifest_file
                    (exit_code, exec_logs) = armada_svc.exec_run(cmd)
                    if exit_code == 0:
                        LOG.info("Manifest file %s was successfully validated." %
                                 manifest_file)
                    else:
                        rc = False
                        if exit_code == CONTAINER_ABNORMAL_EXIT_CODE:
                            LOG.error("Failed to validate application manifest %s. "
                                      "Armada service has exited abnormally." %
                                      manifest_file)
                        else:
                            LOG.error("Failed to validate application manifest "
                                      "%s: %s." % (manifest_file, exec_logs))
                elif request == constants.APP_APPLY_OP:
                    cmd = ("/bin/bash -c 'set -o pipefail; armada apply "
                           "--enable-chart-cleanup --debug {m} {o} {t} | "
                           "tee {l}'".format(m=manifest_file, o=overrides_str,
                                             t=tiller_host, l=logfile))
                    LOG.info("Armada apply command = %s" % cmd)
                    (exit_code, exec_logs) = armada_svc.exec_run(cmd)
                    if exit_code == 0:
                        LOG.info("Application manifest %s was successfully "
                                 "applied/re-applied." % manifest_file)
                    else:
                        rc = False
                        if exit_code == CONTAINER_ABNORMAL_EXIT_CODE:
                            LOG.error("Failed to apply application manifest %s. "
                                      "Armada service has exited abnormally." %
                                      manifest_file)
                        else:
                            LOG.error("Failed to apply application manifest %s. See "
                                      "/var/log/armada/%s for details." %
                                      (manifest_file, os.path.basename(logfile)))
                elif request == constants.APP_ROLLBACK_OP:
                    cmd_rm = "rm " + logfile
                    armada_svc.exec_run(cmd_rm)

                    for app_release in app_releases:
                        release = app_release.get('release')
                        version = app_release.get('version')
                        sequenced = app_release.get('sequenced')

                        if sequenced:
                            cmd = "/bin/bash -c 'set -o pipefail; armada rollback " +\
                                  "--debug --wait --timeout 1800 --release " +\
                                  release + " --version " + str(version) + tiller_host +\
                                  " | tee -a " + logfile + "'"
                        else:
                            cmd = "/bin/bash -c 'set -o pipefail; armada rollback " +\
                                  "--debug --release " + release + " --version " +\
                                  str(version) + tiller_host + " | tee -a " + logfile + "'"
                        (exit_code, exec_logs) = armada_svc.exec_run(cmd)
                        if exit_code != 0:
                            rc = False
                            if exit_code == CONTAINER_ABNORMAL_EXIT_CODE:
                                LOG.error("Failed to rollback release (%s). "
                                          "Armada service has exited abnormally."
                                          % release)
                            else:
                                LOG.error("Failed to rollback release %s. See  "
                                          "/var/log/armada/%s for details." %
                                          (release, os.path.basename(logfile)))
                            break
                    if rc:
                        LOG.info("Application releases %s were successfully "
                                 "rolled back." % app_releases)
                elif request == constants.APP_DELETE_OP:
                    # Since armada delete doesn't support --values overrides
                    # files, use the delete manifest generated from the
                    # ArmadaManifestOperator during overrides generation. It
                    # will contain an accurate view of what was applied
                    manifest_delete_file = "%s-del%s" % os.path.splitext(manifest_file)
                    cmd = "/bin/bash -c 'set -o pipefail; armada delete --debug " +\
                          "--manifest " + manifest_delete_file + tiller_host + " | tee " +\
                          logfile + "'"
                    LOG.info("Armada delete command = %s" % cmd)
                    (exit_code, exec_logs) = armada_svc.exec_run(cmd)
                    if exit_code == 0:
                        LOG.info("Application charts were successfully "
                                 "deleted with manifest %s." % manifest_delete_file)
                    else:
                        rc = False
                        if exit_code == CONTAINER_ABNORMAL_EXIT_CODE:
                            LOG.error("Failed to delete application manifest %s. "
                                      "Armada service has exited abnormally." %
                                      manifest_file)
                        else:
                            LOG.error("Failed to delete application manifest %s. See "
                                      "/var/log/armada/%s for details." %
                                      (manifest_file, os.path.basename(logfile)))
                else:
                    rc = False
                    LOG.error("Unsupported armada request: %s." % request)
            else:
                # Armada sevice failed to start/restart
                rc = False
        except Exception as e:
            # Failed to get a docker client
            rc = False
            LOG.error("Armada request %s for manifest %s failed: %s " %
                      (request, manifest_file, e))
        return rc

    def stop_armada_request(self):
        """A simple way to cancel an on-going manifest apply/rollback/delete
           request. This logic will be revisited in the future.
        """

        try:
            client = docker.from_env(timeout=INSTALLATION_TIMEOUT)
            container = client.containers.get(ARMADA_CONTAINER_NAME)
            if container.status == 'running':
                LOG.info("Stopping Armada service...")
                container.stop()
        except Exception as e:
            # Failed to get a docker client
            LOG.error("Failed to stop Armada service : %s " % e)

    def download_an_image(self, app_name, local_registry_auth, img_tag):

        rc = True

        start = time.time()
        if img_tag.startswith(constants.DOCKER_REGISTRY_HOST):
            try:
                if AppOperator.is_app_aborted(app_name):
                    LOG.info("User aborted. Skipping download of image %s " % img_tag)
                    return img_tag, False

                LOG.info("Image %s download started from local registry" % img_tag)
                client = docker.APIClient(timeout=INSTALLATION_TIMEOUT)
                client.pull(img_tag, auth_config=local_registry_auth)
            except docker.errors.NotFound:
                try:
                    # Pull the image from the public/private registry
                    LOG.info("Image %s is not available in local registry, "
                             "download started from public/private registry"
                             % img_tag)
                    pub_img_tag = img_tag.replace(
                        constants.DOCKER_REGISTRY_SERVER + "/", "")
                    target_img_tag, registry_auth = self._get_img_tag_with_registry(pub_img_tag)
                    client.pull(target_img_tag, auth_config=registry_auth)
                except Exception as e:
                    rc = False
                    LOG.error("Image %s download failed from public/private"
                              "registry: %s" % (target_img_tag, e))
                    return img_tag, rc

                try:
                    # Tag and push the image to the local registry
                    client.tag(target_img_tag, img_tag)
                    client.push(img_tag, auth_config=local_registry_auth)
                except Exception as e:
                    rc = False
                    LOG.error("Image %s push failed to local registry: %s" % (img_tag, e))
            except Exception as e:
                rc = False
                LOG.error("Image %s download failed from local registry: %s" % (img_tag, e))

        else:
            try:
                LOG.info("Image %s download started from public/private registry" % img_tag)
                client = docker.APIClient(timeout=INSTALLATION_TIMEOUT)
                target_img_tag, registry_auth = self._get_img_tag_with_registry(img_tag)
                client.pull(target_img_tag, auth_config=registry_auth)
                client.tag(target_img_tag, img_tag)
            except Exception as e:
                rc = False
                LOG.error("Image %s download failed from public/private registry: %s" % (img_tag, e))

        elapsed_time = time.time() - start
        if rc:
            LOG.info("Image %s download succeeded in %d seconds" %
                     (img_tag, elapsed_time))
        return img_tag, rc
