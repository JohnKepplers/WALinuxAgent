# Microsoft Azure Linux Agent
#
# Copyright 2020 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Requires Python 2.6+ and Openssl 1.0+

import json
import os
import re
import time
from collections import defaultdict

import azurelinuxagent.common.conf as conf
import azurelinuxagent.common.logger as logger
from azurelinuxagent.common.AgentGlobals import AgentGlobals
from azurelinuxagent.common.datacontract import set_properties
from azurelinuxagent.common.event import add_event, WALAEventOperation
from azurelinuxagent.common.exception import IncompleteGoalStateError
from azurelinuxagent.common.exception import ProtocolError, ExtensionConfigError
from azurelinuxagent.common.future import ustr
from azurelinuxagent.common.protocol.restapi import Cert, CertList, Extension, ExtHandler, ExtHandlerList, \
    ExtHandlerVersionUri, RemoteAccessUser, RemoteAccessUsersList, VMAgentManifest, VMAgentManifestList, \
    VMAgentManifestUri, InVMGoalStateMetaData, RequiredFeature, ExtensionState
from azurelinuxagent.common.utils import fileutil
from azurelinuxagent.common.utils.cryptutil import CryptUtil
from azurelinuxagent.common.utils.textutil import parse_doc, findall, find, findtext, getattrib, gettext
from azurelinuxagent.common.version import AGENT_NAME

GOAL_STATE_URI = "http://{0}/machine/?comp=goalstate"
CERTS_FILE_NAME = "Certificates.xml"
P7M_FILE_NAME = "Certificates.p7m"
PEM_FILE_NAME = "Certificates.pem"
TRANSPORT_CERT_FILE_NAME = "TransportCert.pem"
TRANSPORT_PRV_FILE_NAME = "TransportPrivate.pem"

_NUM_GS_FETCH_RETRIES = 6


class GoalState(object):
    def __init__(self, wire_client):
        """
        Fetches the goal state using the given wire client.

        __init__ fetches only the goal state itself, not including inner properties such as ExtensionsConfig; to fetch the entire goal state
        use the fetch_full_goal_state().
        """
        uri = GOAL_STATE_URI.format(wire_client.get_endpoint())

        for _ in range(0, _NUM_GS_FETCH_RETRIES):
            self.xml_text = wire_client.fetch_config(uri, wire_client.get_header())
            xml_doc = parse_doc(self.xml_text)
            self.incarnation = findtext(xml_doc, "Incarnation")

            role_instance = find(xml_doc, "RoleInstance")
            if role_instance:
                break
            time.sleep(0.5)
        else:
            raise IncompleteGoalStateError("Fetched goal state without a RoleInstance [incarnation {inc}]".format(inc=self.incarnation))

        try:
            self.role_instance_id = findtext(role_instance, "InstanceId")
            role_config = find(role_instance, "Configuration")
            self.role_config_name = findtext(role_config, "ConfigName")
            container = find(xml_doc, "Container")
            self.container_id = findtext(container, "ContainerId")

            AgentGlobals.update_container_id(self.container_id)

            # these properties are populated by fetch_full_goal_state()
            self._hosting_env_uri = findtext(xml_doc, "HostingEnvironmentConfig")
            self.hosting_env = None
            self._shared_conf_uri = findtext(xml_doc, "SharedConfig")
            self.shared_conf = None
            self._certs_uri = findtext(xml_doc, "Certificates")
            self.certs = None
            self._ext_conf_uri = findtext(xml_doc, "ExtensionsConfig")
            self.ext_conf = None
            self._remote_access_uri = findtext(container, "RemoteAccessInfo")
            self.remote_access = None

        except Exception as exception:
            # We don't log the error here since fetching the goal state is done every few seconds
            raise ProtocolError(msg="Error fetching goal state", inner=exception)

    def fetch_full_goal_state(self, wire_client):
        try:
            logger.info('Fetching goal state [incarnation {0}]', self.incarnation)

            xml_text = wire_client.fetch_config(self._hosting_env_uri, wire_client.get_header())
            self.hosting_env = HostingEnv(xml_text)

            xml_text = wire_client.fetch_config(self._shared_conf_uri, wire_client.get_header())
            self.shared_conf = SharedConfig(xml_text)

            if self._certs_uri is not None:
                xml_text = wire_client.fetch_config(self._certs_uri, wire_client.get_header_for_cert())
                self.certs = Certificates(xml_text)

            if self._ext_conf_uri is None:
                self.ext_conf = ExtensionsConfig(None)
            else:
                xml_text = wire_client.fetch_config(self._ext_conf_uri, wire_client.get_header())
                self.ext_conf = ExtensionsConfig(xml_text)

            if self._remote_access_uri is not None:
                xml_text = wire_client.fetch_config(self._remote_access_uri, wire_client.get_header_for_cert())
                self.remote_access = RemoteAccess(xml_text)
        except Exception as exception:
            logger.warn("Fetching the goal state failed: {0}", ustr(exception))
            raise ProtocolError(msg="Error fetching goal state", inner=exception)
        finally:
            logger.info('Fetch goal state completed')


class HostingEnv(object):
    def __init__(self, xml_text):
        self.xml_text = xml_text
        xml_doc = parse_doc(xml_text)
        incarnation = find(xml_doc, "Incarnation")
        self.vm_name = getattrib(incarnation, "instance")
        role = find(xml_doc, "Role")
        self.role_name = getattrib(role, "name")
        deployment = find(xml_doc, "Deployment")
        self.deployment_name = getattrib(deployment, "name")


class SharedConfig(object):
    def __init__(self, xml_text):
        self.xml_text = xml_text


class Certificates(object):
    def __init__(self, xml_text):
        self.cert_list = CertList()

        # Save the certificates
        local_file = os.path.join(conf.get_lib_dir(), CERTS_FILE_NAME)
        fileutil.write_file(local_file, xml_text)

        # Separate the certificates into individual files.
        xml_doc = parse_doc(xml_text)
        data = findtext(xml_doc, "Data")
        if data is None:
            return

        # if the certificates format is not Pkcs7BlobWithPfxContents do not parse it
        certificateFormat = findtext(xml_doc, "Format")
        if certificateFormat and certificateFormat != "Pkcs7BlobWithPfxContents":
            logger.warn("The Format is not Pkcs7BlobWithPfxContents. Format is " + certificateFormat)
            return

        cryptutil = CryptUtil(conf.get_openssl_cmd())
        p7m_file = os.path.join(conf.get_lib_dir(), P7M_FILE_NAME)
        p7m = ("MIME-Version:1.0\n"  # pylint: disable=W1308
               "Content-Disposition: attachment; filename=\"{0}\"\n"
               "Content-Type: application/x-pkcs7-mime; name=\"{1}\"\n"
               "Content-Transfer-Encoding: base64\n"
               "\n"
               "{2}").format(p7m_file, p7m_file, data)

        fileutil.write_file(p7m_file, p7m)

        trans_prv_file = os.path.join(conf.get_lib_dir(), TRANSPORT_PRV_FILE_NAME)
        trans_cert_file = os.path.join(conf.get_lib_dir(), TRANSPORT_CERT_FILE_NAME)
        pem_file = os.path.join(conf.get_lib_dir(), PEM_FILE_NAME)
        # decrypt certificates
        cryptutil.decrypt_p7m(p7m_file, trans_prv_file, trans_cert_file, pem_file)

        # The parsing process use public key to match prv and crt.
        buf = []
        begin_crt = False  # pylint: disable=W0612
        begin_prv = False  # pylint: disable=W0612
        prvs = {}
        thumbprints = {}
        index = 0
        v1_cert_list = []
        with open(pem_file) as pem:
            for line in pem.readlines():
                buf.append(line)
                if re.match(r'[-]+BEGIN.*KEY[-]+', line):
                    begin_prv = True
                elif re.match(r'[-]+BEGIN.*CERTIFICATE[-]+', line):
                    begin_crt = True
                elif re.match(r'[-]+END.*KEY[-]+', line):
                    tmp_file = Certificates._write_to_tmp_file(index, 'prv', buf)
                    pub = cryptutil.get_pubkey_from_prv(tmp_file)
                    prvs[pub] = tmp_file
                    buf = []
                    index += 1
                    begin_prv = False
                elif re.match(r'[-]+END.*CERTIFICATE[-]+', line):
                    tmp_file = Certificates._write_to_tmp_file(index, 'crt', buf)
                    pub = cryptutil.get_pubkey_from_crt(tmp_file)
                    thumbprint = cryptutil.get_thumbprint_from_crt(tmp_file)
                    thumbprints[pub] = thumbprint
                    # Rename crt with thumbprint as the file name
                    crt = "{0}.crt".format(thumbprint)
                    v1_cert_list.append({
                        "name": None,
                        "thumbprint": thumbprint
                    })
                    os.rename(tmp_file, os.path.join(conf.get_lib_dir(), crt))
                    buf = []
                    index += 1
                    begin_crt = False

        # Rename prv key with thumbprint as the file name
        for pubkey in prvs:
            thumbprint = thumbprints[pubkey]
            if thumbprint:
                tmp_file = prvs[pubkey]
                prv = "{0}.prv".format(thumbprint)
                os.rename(tmp_file, os.path.join(conf.get_lib_dir(), prv))
                logger.info("Found private key matching thumbprint {0}".format(thumbprint))
            else:
                # Since private key has *no* matching certificate,
                # it will not be named correctly
                logger.warn("Found NO matching cert/thumbprint for private key!")

        # Log if any certificates were found without matching private keys
        # This can happen (rarely), and is useful to know for debugging
        for pubkey in thumbprints:
            if not pubkey in prvs:
                msg = "Certificate with thumbprint {0} has no matching private key."
                logger.info(msg.format(thumbprints[pubkey]))

        for v1_cert in v1_cert_list:
            cert = Cert()
            set_properties("certs", cert, v1_cert)
            self.cert_list.certificates.append(cert)

    @staticmethod
    def _write_to_tmp_file(index, suffix, buf):
        file_name = os.path.join(conf.get_lib_dir(), "{0}.{1}".format(index, suffix))
        fileutil.write_file(file_name, "".join(buf))
        return file_name


class ExtensionsConfig(object):
    def __init__(self, xml_text):
        self.xml_text = xml_text
        self.ext_handlers = ExtHandlerList()
        self.vmagent_manifests = VMAgentManifestList()
        self.in_vm_gs_metadata = InVMGoalStateMetaData()
        self.status_upload_blob = None
        self.status_upload_blob_type = None
        self.artifacts_profile_blob = None
        self.required_features = []

        if xml_text is None:
            return

        xml_doc = parse_doc(self.xml_text)

        ga_families_list = find(xml_doc, "GAFamilies")
        ga_families = findall(ga_families_list, "GAFamily")

        for ga_family in ga_families:
            family = findtext(ga_family, "Name")
            uris_list = find(ga_family, "Uris")
            uris = findall(uris_list, "Uri")
            manifest = VMAgentManifest()
            manifest.family = family
            for uri in uris:
                manifest_uri = VMAgentManifestUri(uri=gettext(uri))
                manifest.versionsManifestUris.append(manifest_uri)
            self.vmagent_manifests.vmAgentManifests.append(manifest)

        self.__parse_plugins_and_settings_and_populate_ext_handlers(xml_doc)

        required_features_list = find(xml_doc, "RequiredFeatures")
        if required_features_list is not None:
            self._parse_required_features(required_features_list)

        self.status_upload_blob = findtext(xml_doc, "StatusUploadBlob")
        self.artifacts_profile_blob = findtext(xml_doc, "InVMArtifactsProfileBlob")

        status_upload_node = find(xml_doc, "StatusUploadBlob")
        self.status_upload_blob_type = getattrib(status_upload_node, "statusBlobType")
        logger.verbose("Extension config shows status blob type as [{0}]", self.status_upload_blob_type)

        self.in_vm_gs_metadata.parse_node(find(xml_doc, "InVMGoalStateMetaData"))

    def _parse_required_features(self, required_features_list):
        for required_feature in findall(required_features_list, "RequiredFeature"):
            feature_name = findtext(required_feature, "Name")
            feature_value = findtext(required_feature, "Value")
            self.required_features.append(RequiredFeature(name=feature_name, value=feature_value))

    def get_redacted_xml_text(self):
        if self.xml_text is None:
            return "<None/>"
        xml_text = self.xml_text
        for ext_handler in self.ext_handlers.extHandlers:
            for extension in ext_handler.properties.extensions:
                if extension.protectedSettings is not None:
                    xml_text = xml_text.replace(extension.protectedSettings, "*** REDACTED ***")
        return xml_text

    def __parse_plugins_and_settings_and_populate_ext_handlers(self, xml_doc):
        """
        Sample ExtensionConfig Plugin and PluginSettings:

        <Plugins>
          <Plugin name="Microsoft.CPlat.Core.NullSeqB" version="2.0.1" location="https://zrdfepirv2cbn04prdstr01a.blob.core.windows.net/f72653efd9e349ed9842c8b99e4c1712/Microsoft.CPlat.Core_NullSeqB_useast2euap_manifest.xml" state="enabled" autoUpgrade="false" failoverlocation="https://zrdfepirv2cbz06prdstr01a.blob.core.windows.net/f72653efd9e349ed9842c8b99e4c1712/Microsoft.CPlat.Core_NullSeqB_useast2euap_manifest.xml" runAsStartupTask="false" isJson="true" useExactVersion="true" />
          <Plugin name="Microsoft.CPlat.Core.NullSeqA" version="2.0.1" location="https://zrdfepirv2cbn04prdstr01a.blob.core.windows.net/f72653efd9e349ed9842c8b99e4c1712/Microsoft.CPlat.Core_NullSeqA_useast2euap_manifest.xml" state="enabled" autoUpgrade="false" failoverlocation="https://zrdfepirv2cbn06prdstr01a.blob.core.windows.net/f72653efd9e349ed9842c8b99e4c1712/Microsoft.CPlat.Core_NullSeqA_useast2euap_manifest.xml" runAsStartupTask="false" isJson="true" useExactVersion="true" />
        </Plugins>
        <PluginSettings>
          <Plugin name="Microsoft.CPlat.Core.NullSeqA" version="2.0.1">
            <DependsOn dependencyLevel="1">
              <DependsOnExtension handler="Microsoft.CPlat.Core.NullSeqB" />
            </DependsOn>
            <RuntimeSettings seqNo="0">{
              "runtimeSettings": [
                {
                  "handlerSettings": {
                    "publicSettings": {"01_add_extensions_with_dependency":"ff2a3da6-8e12-4ab6-a4ca-4e3a473ab385"}
                  }
                }
              ]
            }
            </RuntimeSettings>
          </Plugin>
          <Plugin name="Microsoft.CPlat.Core.NullSeqB" version="2.0.1">
            <RuntimeSettings seqNo="0">{
              "runtimeSettings": [
                {
                  "handlerSettings": {
                    "publicSettings": {"01_add_extensions_with_dependency":"2e837740-cf7e-4528-b3a4-241002618f05"}
                  }
                }
              ]
            }
            </RuntimeSettings>
          </Plugin>
        </PluginSettings>
        """

        plugins_list = find(xml_doc, "Plugins")
        plugins = findall(plugins_list, "Plugin")
        plugin_settings_list = find(xml_doc, "PluginSettings")
        plugin_settings = findall(plugin_settings_list, "Plugin")

        for plugin in plugins:
            ext_handler = ExtHandler()
            try:
                ExtensionsConfig._parse_plugin(ext_handler, plugin)
                ExtensionsConfig._parse_plugin_settings(ext_handler, plugin_settings)
            except ExtensionConfigError as error:
                ext_handler.invalid_setting_reason = ustr(error)

            self.ext_handlers.extHandlers.append(ext_handler)

    @staticmethod
    def _parse_plugin(ext_handler, plugin):
        """
        Sample config:

        <Plugins>
          <Plugin name="Microsoft.CPlat.Core.NullSeqB" version="2.0.1" location="https://zrdfepirv2cbn04prdstr01a.blob.core.windows.net/f72653efd9e349ed9842c8b99e4c1712/Microsoft.CPlat.Core_NullSeqB_useast2euap_manifest.xml" state="enabled" autoUpgrade="false" failoverlocation="https://zrdfepirv2cbz06prdstr01a.blob.core.windows.net/f72653efd9e349ed9842c8b99e4c1712/Microsoft.CPlat.Core_NullSeqB_useast2euap_manifest.xml" runAsStartupTask="false" isJson="true" useExactVersion="true"><Plugin name="Microsoft.Azure.Extensions.CustomScript" version="1.0" location="https://rdfecurrentuswestcache.blob.core.test-cint.azure-test.net/0e53c53ef0be4178bacb0a1fecf12a74/Microsoft.Azure.Extensions_CustomScript_usstagesc_manifest.xml" state="enabled" autoUpgrade="false" failoverlocation="https://rdfecurrentuswestcache2.blob.core.test-cint.azure-test.net/0e53c53ef0be4178bacb0a1fecf12a74/Microsoft.Azure.Extensions_CustomScript_usstagesc_manifest.xml" runAsStartupTask="false" isJson="true" useExactVersion="true">
            <additionalLocations>
              <additionalLocation>https://rdfecurrentuswestcache3.blob.core.test-cint.azure-test.net/0e53c53ef0be4178bacb0a1fecf12a74/Microsoft.Azure.Extensions_CustomScript_usstagesc_manifest.xml</additionalLocation>
              <additionalLocation>https://rdfecurrentuswestcache4.blob.core.test-cint.azure-test.net/0e53c53ef0be4178bacb0a1fecf12a74/Microsoft.Azure.Extensions_CustomScript_usstagesc_manifest.xml</additionalLocation>
            </additionalLocations>
          </Plugin>
          <Plugin name="Microsoft.CPlat.Core.NullSeqA" version="2.0.1" location="https://zrdfepirv2cbn04prdstr01a.blob.core.windows.net/f72653efd9e349ed9842c8b99e4c1712/Microsoft.CPlat.Core_NullSeqA_useast2euap_manifest.xml" state="enabled" autoUpgrade="false" failoverlocation="https://zrdfepirv2cbn06prdstr01a.blob.core.windows.net/f72653efd9e349ed9842c8b99e4c1712/Microsoft.CPlat.Core_NullSeqA_useast2euap_manifest.xml" runAsStartupTask="false" isJson="true" useExactVersion="true" />
        </Plugins>

        
        Note that the `additionalLocations` subnode is populated with links
        generated by PIR for resiliency. In regions with this feature enabled,
        CRP will provide any extra links in the format above. If no extra links
        are provided, the subnode will not exist.
        """

        def _log_error_if_none(attr_name, value):
            # Plugin Name and Version are very essential fields, without them we wont be able to even report back to CRP
            # about that handler. For those cases we need to fail the GoalState completely but currently we dont support
            # reporting status at a GoalState level (we only report at a handler level).
            # Once that functionality is added to the GA, we would raise here rather than just report error in our logs.
            if value in (None, ""):
                add_event(AGENT_NAME, op=WALAEventOperation.InvalidExtensionConfig,
                          message="{0} is None for ExtensionConfig, logging error".format(attr_name),
                          log_event=True, is_success=False)
            return value

        ext_handler.name = _log_error_if_none("Extensions.Plugins.Plugin.name", getattrib(plugin, "name"))
        ext_handler.properties.version = _log_error_if_none("Extensions.Plugins.Plugin.version",
                                                            getattrib(plugin, "version"))
        ext_handler.properties.state = getattrib(plugin, "state")
        if ext_handler.properties.state in (None, ""):
            raise ExtensionConfigError("Received empty Extensions.Plugins.Plugin.state, failing Handler")

        def getattrib_wrapped_in_list(node, attr_name):
            attr = getattrib(node, attr_name)
            return [attr] if attr not in (None, "") else []

        location = getattrib_wrapped_in_list(plugin, "location")
        failover_location = getattrib_wrapped_in_list(plugin, "failoverlocation")

        locations = location + failover_location

        additional_location_node = find(plugin, "additionalLocations")
        if additional_location_node is not None:
            nodes_list = findall(additional_location_node, "additionalLocation")
            locations += [gettext(node) for node in nodes_list]
        
        for uri in locations:
            version_uri = ExtHandlerVersionUri()
            version_uri.uri = uri
            ext_handler.versionUris.append(version_uri)

    @staticmethod
    def _parse_plugin_settings(ext_handler, plugin_settings):
        """
        Sample config:

        <PluginSettings>
            <Plugin name="Microsoft.CPlat.Core.NullSeqA" version="2.0.1">
                <DependsOn dependencyLevel="1">
                  <DependsOnExtension handler="Microsoft.CPlat.Core.NullSeqB" />
                </DependsOn>
                <RuntimeSettings seqNo="0">{
                  "runtimeSettings": [
                    {
                      "handlerSettings": {
                        "publicSettings": {"01_add_extensions_with_dependency":"ff2a3da6-8e12-4ab6-a4ca-4e3a473ab385"}
                      }
                    }
                  ]
                }
                </RuntimeSettings>
            </Plugin>
            <Plugin name="Microsoft.CPlat.Core.RunCommandHandlerWindows" version="2.0.2">
                <ExtensionRuntimeSettings seqNo="4" name="firstRunCommand" state="enabled">{
                  "runtimeSettings": [
                    {
                      "handlerSettings": {
                        "publicSettings": {"source":{"script":"Write-Host First: Hello World TestTry2!"},"parameters":[{"name":"extensionName","value":"firstRunCommand"}],"timeoutInSeconds":120}
                      }
                    }
                  ]
                }
                </ExtensionRuntimeSettings>
            </Plugin>
        </PluginSettings>
        """
        if plugin_settings is None:
            return

        handler_name = ext_handler.name
        version = ext_handler.properties.version

        def to_lower(str_to_change): return str_to_change.lower() if str_to_change is not None else None

        ext_handler_plugin_settings = [x for x in plugin_settings if to_lower(getattrib(x, "name")) == to_lower(handler_name)]
        if not ext_handler_plugin_settings:
            return

        settings = [x for x in ext_handler_plugin_settings if getattrib(x, "version") == version]
        if len(settings) != len(ext_handler_plugin_settings):
            msg = "ExtHandler PluginSettings Version Mismatch! Expected PluginSettings version: {0} for Handler: {1} but found versions: ({2})".format(
                version, handler_name, ', '.join(set([getattrib(x, "version") for x in ext_handler_plugin_settings])))
            add_event(AGENT_NAME, op=WALAEventOperation.PluginSettingsVersionMismatch, message=msg, log_event=True,
                      is_success=False)
            raise ExtensionConfigError(msg)

        if len(settings) > 1:
            msg = "Multiple plugin settings found for the same handler: {0} and version: {1} (Expected: 1; Available: {2})".format(
                handler_name, version, len(settings))
            raise ExtensionConfigError(msg)

        plugin_settings_node = settings[0]
        runtime_settings_nodes = findall(plugin_settings_node, "RuntimeSettings")
        extension_runtime_settings_nodes = findall(plugin_settings_node, "ExtensionRuntimeSettings")

        if any(runtime_settings_nodes) and any(extension_runtime_settings_nodes):
            # There can only be a single RuntimeSettings node or multiple ExtensionRuntimeSettings nodes per Plugin
            msg = "Both RuntimeSettings and ExtensionRuntimeSettings found for the same handler: {0} and version: {1}".format(
                handler_name, version)
            raise ExtensionConfigError(msg)

        if runtime_settings_nodes:
            if len(runtime_settings_nodes) > 1:
                msg = "Multiple RuntimeSettings found for the same handler: {0} and version: {1} (Expected: 1; Available: {2})".format(
                    handler_name, version, len(runtime_settings_nodes))
                raise ExtensionConfigError(msg)
            # Only Runtime settings available, parse that
            ExtensionsConfig.__parse_runtime_settings(plugin_settings_node, runtime_settings_nodes[0], handler_name,
                                                      ext_handler)
        elif extension_runtime_settings_nodes:
            # Parse the ExtensionRuntime settings for the given extension
            ExtensionsConfig.__parse_extension_runtime_settings(plugin_settings_node, extension_runtime_settings_nodes,
                                                                ext_handler)

    @staticmethod
    def __get_dependency_level_from_node(depends_on_node, name):
        depends_on_level = 0
        if depends_on_node is not None:
            try:
                depends_on_level = int(getattrib(depends_on_node, "dependencyLevel"))
            except (ValueError, TypeError):
                logger.warn("Could not parse dependencyLevel for handler {0}. Setting it to 0".format(name))
                depends_on_level = 0
        return depends_on_level

    @staticmethod
    def __parse_runtime_settings(plugin_settings_node, runtime_settings_node, handler_name, ext_handler):
        """
        Sample Plugin in PluginSettings containing DependsOn and RuntimeSettings (single settings per extension) -

        <Plugin name="Microsoft.Compute.VMAccessAgent" version="2.4.7">
        <DependsOn dependencyLevel="2">
          <DependsOnExtension extension="firstRunCommand" handler="Microsoft.CPlat.Core.RunCommandHandlerWindows" />
          <DependsOnExtension handler="Microsoft.Compute.CustomScriptExtension" />
        </DependsOn>
        <RuntimeSettings seqNo="1">{
              "runtimeSettings": [
                {
                  "handlerSettings": {
                    "protectedSettingsCertThumbprint": "<Redacted>",
                    "protectedSettings": "<Redacted>",
                    "publicSettings": {"UserName":"test1234"}
                  }
                }
              ]
            }
        </RuntimeSettings>
        </Plugin>
        """
        depends_on_nodes = findall(plugin_settings_node, "DependsOn")
        if len(depends_on_nodes) > 1:
            msg = "Extension Handler can only have a single dependsOn node for Single config extensions. Found: {0}".format(
                len(depends_on_nodes))
            raise ExtensionConfigError(msg)
        depends_on_node = depends_on_nodes[0] if depends_on_nodes else None
        depends_on_level = ExtensionsConfig.__get_dependency_level_from_node(depends_on_node, handler_name)
        ExtensionsConfig.__parse_and_add_extension_settings(runtime_settings_node, handler_name, ext_handler,
                                                            depends_on_level)

    @staticmethod
    def __parse_extension_runtime_settings(plugin_settings_node, extension_runtime_settings_nodes, ext_handler):
        """
        Sample PluginSettings containing DependsOn and ExtensionRuntimeSettings -

        <Plugin name="Microsoft.CPlat.Core.RunCommandHandlerWindows" version="2.0.2">
        <DependsOn dependencyLevel="3" name="secondRunCommand">
          <DependsOnExtension extension="firstRunCommand" handler="Microsoft.CPlat.Core.RunCommandHandlerWindows" />
          <DependsOnExtension handler="Microsoft.Compute.CustomScriptExtension" />
          <DependsOnExtension handler="Microsoft.Compute.VMAccessAgent" />
        </DependsOn>
        <DependsOn dependencyLevel="4" name="thirdRunCommand">
          <DependsOnExtension extension="firstRunCommand" handler="Microsoft.CPlat.Core.RunCommandHandlerWindows" />
          <DependsOnExtension extension="secondRunCommand" handler="Microsoft.CPlat.Core.RunCommandHandlerWindows" />
          <DependsOnExtension handler="Microsoft.Compute.CustomScriptExtension" />
          <DependsOnExtension handler="Microsoft.Compute.VMAccessAgent" />
        </DependsOn>
        <ExtensionRuntimeSettings seqNo="2" name="firstRunCommand" state="enabled">
            {
              "runtimeSettings": [
                {
                  "handlerSettings": {
                    "publicSettings": {"source":{"script":"Write-Host First: Hello World 1234!"}}
                  }
                }
              ]
            }
        </ExtensionRuntimeSettings>
        <ExtensionRuntimeSettings seqNo="2" name="secondRunCommand" state="enabled">
            {
              "runtimeSettings": [
                {
                  "handlerSettings": {
                    "publicSettings": {"source":{"script":"Write-Host First: Hello World 1234!"}}
                  }
                }
              ]
            }
        </ExtensionRuntimeSettings>
        <ExtensionRuntimeSettings seqNo="1" name="thirdRunCommand" state="enabled">
            {
              "runtimeSettings": [
                {
                  "handlerSettings": {
                    "publicSettings": {"source":{"script":"Write-Host Third: Hello World 3!"}}
                  }
                }
              ]
            }
        </ExtensionRuntimeSettings>
      </Plugin>
        """
        # Parse and cache the Dependencies for each extension first
        dependency_levels = defaultdict(int)
        for depends_on_node in findall(plugin_settings_node, "DependsOn"):
            extension_name = getattrib(depends_on_node, "name")
            if extension_name in (None, ""):
                raise ExtensionConfigError("No Name not specified for DependsOn object in ExtensionRuntimeSettings for MultiConfig!")

            dependency_level = ExtensionsConfig.__get_dependency_level_from_node(depends_on_node, extension_name)
            dependency_levels[extension_name] = dependency_level

        ext_handler.supports_multi_config = True
        for extension_runtime_setting_node in extension_runtime_settings_nodes:
            # Name and State will only be set for ExtensionRuntimeSettings for Multi-Config
            extension_name = getattrib(extension_runtime_setting_node, "name")
            if extension_name in (None, ""):
                raise ExtensionConfigError("Extension Name not specified for ExtensionRuntimeSettings for MultiConfig!")
            # State can either be `ExtensionState.Enabled` (default) or `ExtensionState.Disabled`
            state = getattrib(extension_runtime_setting_node, "state")
            state = ustr(state.lower()) if state not in (None, "") else ExtensionState.Enabled
            ExtensionsConfig.__parse_and_add_extension_settings(extension_runtime_setting_node, extension_name,
                                                                ext_handler, dependency_levels[extension_name],
                                                                state=state)

    @staticmethod
    def __parse_and_add_extension_settings(settings_node, name, ext_handler, depends_on_level, state=ExtensionState.Enabled):
        seq_no = getattrib(settings_node, "seqNo")
        if seq_no in (None, ""):
            raise ExtensionConfigError("SeqNo not specified for the Extension: {0}".format(name))

        try:
            runtime_settings = json.loads(gettext(settings_node))
        except ValueError as error:
            logger.error("Invalid extension settings: {0}", ustr(error))
            # Incase of invalid/no settings, add the name and seqNo of the Extension and treat it as an extension with
            # no settings since we were able to successfully parse those data properly. Without this, we wont report
            # anything for that sequence number and CRP would eventually have to timeout rather than fail fast.
            ext_handler.properties.extensions.append(
                Extension(name=name, sequenceNumber=seq_no, state=state, dependencyLevel=depends_on_level))
            return

        for plugin_settings_list in runtime_settings["runtimeSettings"]:
            handler_settings = plugin_settings_list["handlerSettings"]
            ext = Extension()
            # There is no "extension name" for single Handler Settings. Use HandlerName for those
            ext.name = name
            ext.state = state
            ext.sequenceNumber = seq_no
            ext.publicSettings = handler_settings.get("publicSettings")
            ext.protectedSettings = handler_settings.get("protectedSettings")
            ext.dependencyLevel = depends_on_level
            thumbprint = handler_settings.get("protectedSettingsCertThumbprint")
            ext.certificateThumbprint = thumbprint
            ext_handler.properties.extensions.append(ext)


class RemoteAccess(object):
    """
    Object containing information about user accounts
    """
    #
    # <RemoteAccess>
    #   <Version/>
    #   <Incarnation/>
    #    <Users>
    #       <User>
    #         <Name/>
    #         <Password/>
    #         <Expiration/>
    #       </User>
    #     </Users>
    #   </RemoteAccess>
    #
    def __init__(self, xml_text):
        self.xml_text = xml_text
        self.version = None
        self.incarnation = None
        self.user_list = RemoteAccessUsersList()

        if self.xml_text is None or len(self.xml_text) == 0:
            return

        xml_doc = parse_doc(self.xml_text)
        self.version = findtext(xml_doc, "Version")
        self.incarnation = findtext(xml_doc, "Incarnation")
        user_collection = find(xml_doc, "Users")
        users = findall(user_collection, "User")

        for user in users:
            remote_access_user = RemoteAccess._parse_user(user)
            self.user_list.users.append(remote_access_user)

    @staticmethod
    def _parse_user(user):
        name = findtext(user, "Name")
        encrypted_password = findtext(user, "Password")
        expiration = findtext(user, "Expiration")
        remote_access_user = RemoteAccessUser(name, encrypted_password, expiration)
        return remote_access_user

