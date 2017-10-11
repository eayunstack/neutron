# Copyright (c) 2017 Eayun, Inc.
# All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc
import six

from neutron.api import extensions
from neutron.api.v2 import attributes as attr
from neutron.api.v2 import resource_helper
from neutron.common import exceptions
from neutron.openstack.common import log as logging
from neutron.plugins.common import constants
from neutron.services import service_base

LOG = logging.getLogger(__name__)

ACL_VALID_ACTION_VALUES = [constants.FWAAS_ALLOW, constants.FWAAS_DENY]


class AclNotFound(exceptions.NotFound):
    message = _("ACL %(acl_id)s could not be found.")


class AclInUse(exceptions.InUse):
    message = _("ACL %(acl_id)s is used by subnets %(subnets)s.")


class AclRuleNotFound(exceptions.NotFound):
    message = _("ACL %(acl_rule_id)s could not be found.")


def _validate_acl_ipaddr(data, valid_values=None):
    if data is None:
        return
    msg_ip = attr._validate_ip_address(data, valid_values)
    if not msg_ip:
        return
    msg_subnet = attr._validate_subnet(data, valid_values)
    if not msg_subnet:
        return
    return "%(msg_ip)s and %(msg_subnet)s" % {'msg_ip': msg_ip,
                                              'msg_subnet': msg_subnet}


def _validate_acl_port_range(data, valid_values=None):
    if data is None:
        return
    data = str(data)
    ports = data.split(':')
    for port in ports:
        try:
            val = int(port)
            if val <= 0 or val > 65535:
                raise ValueError
        except (ValueError, TypeError):
            return "'%s' is not a valid port number." % port

    if len(ports) > 2 or int(ports[0]) > int(ports[-1]):
        return "'%s' is not a valid port range." % data


def _convert_to_string(value):
    return str(value) if value is not None else None


def _convert_to_lower_string(value):
    return value.lower()


attr.validators['type:acl_ipaddr'] = _validate_acl_ipaddr
attr.validators['type:acl_port_range'] = _validate_acl_port_range

RESOURCE_ATTRIBUTE_MAP = {
    'es_acls': {
        'id': {'allow_post': False, 'allow_put': False,
               'is_visible': True, 'primary_key': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'default': '', 'is_visible': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'is_visible': True, 'required_by_policy': True},
        'subnets': {'allow_post': False, 'allow_put': False,
                    'is_visible': True},
        'ingress_rules': {'allow_post': False, 'allow_put': False,
                          'is_visible': True},
        'egress_rules': {'allow_post': False, 'allow_put': False,
                         'is_visible': True},
    },
    'es_acl_rules': {
        'id': {'allow_post': False, 'allow_put': False,
               'is_visible': True, 'primary_key': True},
        'name': {'allow_post': True, 'allow_put': True,
                 'default': '', 'is_visible': True},
        'tenant_id': {'allow_post': True, 'allow_put': False,
                      'is_visible': True, 'required_by_policy': True},
        'acl_id': {'allow_post': True, 'allow_put': True,
                   'default': None, 'validate': {'type:uuid_or_none': None},
                   'is_visible': True},
        'position': {'allow_post': True, 'allow_put': True,
                     'default': None,
                     'validate': {'type:range_or_none': [0, 255]},
                     'convert_to': attr.convert_to_int_if_not_none,
                     'is_visible': True},
        'direction': {'allow_post': True, 'allow_put': True,
                      'validate': {'type:values': ['ingress', 'egress']},
                      'is_visible': True},
        'protocol': {'allow_post': True, 'allow_put': True,
                     'default': None,
                     'validate': {'type:range_or_none': [0, 255]},
                     'convert_to': attr.convert_to_int_if_not_none,
                     'is_visible': True},
        'source_ip_address': {'allow_post': True, 'allow_put': True,
                              'default': None,
                              'validate': {'type:acl_ipaddr': None},
                              'is_visible': True},
        'destination_ip_address': {'allow_post': True, 'allow_put': True,
                                   'default': None,
                                   'validate': {'type:acl_ipaddr': None},
                                   'is_visible': True},
        'source_port': {'allow_post': True, 'allow_put': True,
                        'default': None,
                        'validate': {'type:acl_port_range': None},
                        'convert_to': _convert_to_string,
                        'is_visible': True},
        'destination_port': {'allow_post': True, 'allow_put': True,
                             'default': None,
                             'validate': {'type:acl_port_range': None},
                             'convert_to': _convert_to_string,
                             'is_visible': True},
        'action': {'allow_post': True, 'allow_put': True,
                   'validate': {'type:values': ACL_VALID_ACTION_VALUES},
                   'convert_to': _convert_to_lower_string,
                   'is_visible': True},
    }
}


class Es_acl(extensions.ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "EayunStack Neutron Subnet ACL"

    @classmethod
    def get_alias(cls):
        return "es-acl"

    @classmethod
    def get_description(cls):
        return "Eayunstack Neutron Subnet ACL extension."

    @classmethod
    def get_namespace(cls):
        return "https://github.com/eayunstack"

    @classmethod
    def get_updated(cls):
        return "2017-08-24:00:00-00:00"

    @classmethod
    def get_plugin_interface(cls):
        return EsAclPluginBase

    @classmethod
    def get_resources(cls):
        """Returns Ext Resources."""
        plural_mappings = resource_helper.build_plural_mappings(
            {}, RESOURCE_ATTRIBUTE_MAP)
        attr.PLURALS.update(plural_mappings)
        action_map = {'es_acl': {'bind_subnets': 'PUT',
                                 'unbind_subnets': 'PUT'}}
        return resource_helper.build_resource_info(plural_mappings,
                                                   RESOURCE_ATTRIBUTE_MAP,
                                                   constants.ES_ACL,
                                                   action_map=action_map,
                                                   register_quota=True)

    def update_attributes_map(self, extended_attributes,
                              extension_attrs_map=None):
        super(Es_acl, self).update_attributes_map(
            extended_attributes, extension_attrs_map=RESOURCE_ATTRIBUTE_MAP)

    def get_extended_resources(self, version):
        return RESOURCE_ATTRIBUTE_MAP if version == "2.0" else {}


@six.add_metaclass(abc.ABCMeta)
class EsAclPluginBase(service_base.ServicePluginBase):

    def get_plugin_name(self):
        return constants.ES_ACL

    def get_plugin_description(self):
        return constants.ES_ACL

    def get_plugin_type(self):
        return constants.ES_ACL

    @abc.abstractmethod
    def create_es_acl(self, context, es_acl):
        """Create an EayunStack subnet ACL."""
        pass

    @abc.abstractmethod
    def update_es_acl(self, context, es_acl_id, es_acl):
        """Update an EayunStack subnet ACL."""
        pass

    @abc.abstractmethod
    def delete_es_acl(self, context, es_acl_id):
        """Delete an EayunStack subnet ACL."""
        pass

    @abc.abstractmethod
    def get_es_acl(self, context, es_acl_id, fields=None):
        """Get an EayunStack subnet ACL."""
        pass

    @abc.abstractmethod
    def get_es_acls(self, context, filters=None, fields=None,
                    sorts=None, limit=None, marker=None,
                    page_reverse=False):
        """List EayunStack subnet ACLs."""
        pass

    @abc.abstractmethod
    def bind_subnets(self, context, es_acl_id, subnet_ids):
        """Bind subnets to ACL."""
        pass

    @abc.abstractmethod
    def unbind_subnets(self, context, es_acl_id, subnet_ids):
        """Unbind subnets from ACL."""
        pass

    @abc.abstractmethod
    def create_es_acl_rule(self, context, es_acl_rule):
        """Create an EayunStack subnet ACL rule."""
        pass

    @abc.abstractmethod
    def update_es_acl_rule(self, context, es_acl_rule_id, es_acl_rule):
        """Update an EayunStack subnet ACL rule."""
        pass

    @abc.abstractmethod
    def delete_es_acl_rule(self, context, es_acl_rule_id):
        """Delete an EayunStack subnet ACL rule."""
        pass

    @abc.abstractmethod
    def get_es_acl_rule(self, context, es_acl_rule_id, fields=None):
        """Get an EayunStack subnet ACL rule."""
        pass

    @abc.abstractmethod
    def get_es_acl_rules(self, context, filters=None, fields=None,
                         sorts=None, limit=None, marker=None,
                         page_reverse=False):
        """List EayunStack subnet ACL rules."""
        pass
