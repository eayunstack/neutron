# Copyright 2016 Eayun, Inc.
# All Rights Reserved.
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


import time

from neutron.openstack.common import log as logging
from neutron.agent.linux.ovs_lib import INVALID_OFPORT


LOG = logging.getLogger(__name__)

# Tables for OpenFlow East-West DVR flows of integration bridge
OF_EW_DVR_DST = 1
OF_EW_DVR_SRC = 2


class OFEWDVRAgent(object):
    '''
    Implements OVS-based OpenFlow DVR(Distributed Virtual Router), only for
    inter-subnets(East-to-West) traffic.

    OpenFlow tables design:

    * Table 0:
      - Priority 5
        dl_src is in other hosts' DVR MACs, resubmit to OF_EW_DVR_SRC
        - See setup_dvr_flows_on_integ_tun_br
        - See dvr_mac_address_update
      - Priority 4
        destined from outside to hosted instances, change vlan vid to internal
        vid, NORMAL
      - Priority 3
        other traffic from outside, drop
      - Priority 2
        from instances to instances in other connected subnets via router,
        dl_dst is instance's gateway MAC, ip_dst is the IP address of an
        instance in connected subnet, resubmit to OF_EW_DVR_DST (East-West
        traffic)
        - See _add_flows_to_other_subnets
      - Priority 1
        normal switching of hosted instances in the same subnet
    * Table 1 (OF_EW_DVR_DST):
      - Priority 2
        dl_dst is a subnet's gateway MAC, ip_dst is the IP of an instance in a
        connected subnet, change dl_dst to the instance's MAC, change vlan vid
        to the network's segmentation id of the instance, resubmit to
        OF_EW_DVR_SRC
        - See _add_flows_to_port
      - Priority 1
        normal switch, in case traffic is destined to the router of the subnet
        - See setup_dvr_flows_on_integ_tun_br
    * Table 2 (OF_EW_DVR_SRC):
      - Priority 3
        dl_dst is a hosted instance's MAC, ip_dst is its IP, strip vlan id,
        change dl_src to the instance's gateway MAC, decrease IP ttl, output
        to the hosted port
        - See _add_flows_to_hosted_port
      - Priority 2
        dl_src is in other hosts' DVR MAC, not destined to hosted ports, drop
        - See setup_dvr_flows_on_integ_tun_br
        - See dvr_mac_address_update
      - Priority 1
        traffic from hosted instances destined to instances of connected
        subnets outside this host, change dl_src to this host's DVR MAC and
        output to patch port(s) connecting to the outside
        - See _set_src_to_dvr
    '''
    # history
    #   1.0 Initial version

    def __init__(self, context, plugin_rpc, int_br, int_ofports,
                 host=None, sync_interval=300):
        self.context = context
        self.plugin_rpc = plugin_rpc
        self.int_br = int_br
        self.int_ofports = ','.join(str(num) for num in int_ofports.values())
        self.host = host
        self.sync_interval = sync_interval

    def _set_src_to_dvr(self):
        actions = "mod_dl_src:%s,%s" % (self.dvr_mac_address, self.int_ofports)
        if self.dvr_mac_address:
            self.int_br.add_flow(
                table=OF_EW_DVR_SRC, priority=1,
                actions=actions)

    def _get_dvr_mac_address(self):
        try:
            details = self.plugin_rpc.get_dvr_mac_address_by_host(
                self.context, self.host)
            LOG.debug("L2 Agent OF-EW DVR: Received response for "
                      "get_dvr_mac_address_by_host() from "
                      "plugin: %r", details)
            self.dvr_mac_address = details['mac_address']
        except Exception:
            LOG.error(_("DVR: Failed to obtain local DVR MAC address"))

    def setup_dvr_flows_on_integ_tun_br(self):
        '''Setup up initial dvr flows into br-int and br-tun'''
        # TODO: Currently using this agent with tunneling enabled is
        # not supported. So nothing should be done with br-tun.

        LOG.debug("L2 Agent operating in OpenFlow East-West DVR Mode")
        self.dvr_mac_address = None
        self.registered_dvr_macs = set()

        self.last_sync = 0
        self.registered_routers = dict()

        # Change inter-subnets traffic originating from VMs on this host.
        # Set their SRC_MAC as this host's DVR_MAC.
        self._get_dvr_mac_address()
        self._set_src_to_dvr()

        dvr_macs = self.plugin_rpc.get_dvr_mac_address_list(self.context)
        LOG.debug("L2 Agent OF-EW DVR: Received these MACs: %r", dvr_macs)
        for mac in dvr_macs:
            if (mac['mac_address'] == self.dvr_mac_address or
                    mac['host'] == self.host):
                continue
            # Resubmit traffic with SRC_MAC being set as another host's
            # DVR MAC to table OF_EW_DVR_SRC.
            self.int_br.add_flow(
                priority=5, dl_src=mac['mac_address'],
                actions="resubmit(,%s)" % OF_EW_DVR_SRC)
            # By default block traffic with SRC_MAC being set as another host's
            # DVR MAC.
            self.int_br.add_flow(
                table=OF_EW_DVR_SRC, priority=2, dl_src=mac['mac_address'],
                actions="drop")
            self.registered_dvr_macs.add(mac['mac_address'])

        # Add fallback flow for all unknown IP/DST_MAC pairs
        # Known IP/DST_MAC pairs are those IP is in a connected subnet
        # and DST_MAC is the router_port's MAC address of the originating
        # subnet.
        self.int_br.add_flow(table=OF_EW_DVR_DST, priority=1,
                             actions="normal")

    def setup_dvr_flows_on_phys_brs(self, phys_brs):
        if not self.dvr_mac_address:
            return

        for br in phys_brs.values():
            br.add_flow(
                priority=3, dl_src=self.dvr_mac_address,
                actions='normal')

    def dvr_mac_address_update(self, dvr_macs):
        LOG.debug("DVR MAC address update with host-mac: %s", dvr_macs)
        if not self.dvr_mac_address:
            LOG.debug("Self DVR MAC address unknown, ignoring this "
                      "dvr_mac_address_update()")
            return

        dvr_host_macs = set()
        for mac in dvr_macs:
            if (mac['mac_address'] == self.dvr_mac_address or
                    mac['host'] == self.host):
                continue
            dvr_host_macs.add(mac['mac_address'])

        if dvr_host_macs == self.registered_dvr_macs:
            LOG.debug("DVR MAC address already up to date")
            return

        dvr_macs_added = dvr_host_macs - self.registered_dvr_macs
        dvr_macs_removed = self.registered_dvr_macs - dvr_host_macs

        for oldmac in dvr_macs_removed:
            self.int_br.delete_flows(
                dl_src=oldmac,
                actions="resubmit(,%s)" % OF_EW_DVR_SRC)
            self.int_br.delete_flows(
                table=OF_EW_DVR_SRC, dl_src=oldmac,
                actions="drop")
            LOG.debug("Removed DVR MAC flow for %s", oldmac)
            self.registered_dvr_macs.remove(oldmac)

        for newmac in dvr_macs_added:
            self.int_br.add_flow(
                priority=5, dl_src=newmac,
                actions="resubmit(,%s)" % OF_EW_DVR_SRC)
            self.int_br.add_flow(
                table=OF_EW_DVR_SRC, priority=2, dl_src=newmac,
                actions="drop")
            LOG.debug("Added DVR MAC flow for %s", newmac)
            self.registered_dvr_macs.add(newmac)

    def _add_flows_to_other_subnets(self, gateway_mac, cidrs):
        if not self.dvr_mac_address:
            return
        for cidr in cidrs:
            self.int_br.add_flow(
                priority=2,
                dl_dst=gateway_mac, proto='ip', ip_dst=cidr,
                actions="resubmit(,%s)" % OF_EW_DVR_DST)

    def _del_flows_to_other_subnets(self, gateway_mac, cidrs):
        for cidr in cidrs:
            self.int_br.delete_flows(
                dl_dst=gateway_mac, proto='ip', ip_dst=cidr)

    def _add_flows_to_port(self, gateway_macs, port, seg_id):
        if not self.dvr_mac_address:
            return
        actions = "mod_dl_dst:%s,mod_vlan_vid:%s,resubmit(,%s)" % (
            port['mac'], seg_id, OF_EW_DVR_SRC)
        for gateway_mac in gateway_macs:
            self.int_br.add_flow(
                table=OF_EW_DVR_DST, priority=2,
                dl_dst=gateway_mac, proto='ip', ip_dst=port['ip'],
                actions=actions)

    def _del_flows_to_port(self, gateway_macs, port):
        for gateway_mac in gateway_macs:
            self.int_br.delete_flows(
                table=OF_EW_DVR_DST,
                dl_dst=gateway_mac, proto='ip', ip_dst=port['ip'])

    def _add_flows_to_hosted_port(self, port, gateway_mac):
        if port['host'] != self.host:
            LOG.debug("Port %s is not hosted by this agent.", port['name'])
            return
        port_ofno = self.int_br.get_port_ofport(port['name'])
        if port_ofno == INVALID_OFPORT:
            LOG.warning("Port %s is not ready.", port['name'])
            # Setting port['host'] to None for next sync.
            port['host'] = None
            return
        actions = "strip_vlan,mod_dl_src:%s,dec_ttl,output:%s" % (
            gateway_mac, port_ofno)
        self.int_br.add_flow(
            table=OF_EW_DVR_SRC, priority=3,
            dl_dst=port['mac'], proto='ip', ip_dst=port['ip'],
            actions=actions)

    def _del_flows_to_hosted_port(self, port):
        if port['host'] != self.host:
            return
        self.int_br.delete_flows(
            table=OF_EW_DVR_SRC,
            dl_dst=port['mac'], proto='ip', ip_dst=port['ip'])

    def _subnet_added(self, subnet_id, subnet, cidrs, gateway_macs):
        gateway_mac = subnet['gateway_mac']
        other_cidrs = cidrs - set([subnet['cidr']])
        other_gateway_macs = gateway_macs - set([gateway_mac])

        self._add_flows_to_other_subnets(gateway_mac, other_cidrs)
        for port in subnet['ports'].values():
            self._add_flows_to_port(other_gateway_macs, port,
                                    subnet['seg_id'])
            self._add_flows_to_hosted_port(port, gateway_mac)

    def _subnet_updated(self, subnet_id, subnets, cidrs, gateway_macs):
        old_subnet, subnet = subnets
        old_cidrs, cidrs = cidrs
        old_gateway_macs, gateway_macs = gateway_macs

        gateway_mac = subnet['gateway_mac']
        other_gateway_macs = gateway_macs - set([gateway_mac])

        # delete flows to deleted subnets
        self._del_flows_to_other_subnets(gateway_mac, old_cidrs - cidrs)
        # add flows to added subnets
        self._add_flows_to_other_subnets(gateway_mac, cidrs - old_cidrs)

        for port_id, port in subnet['ports'].iteritems():
            if port_id not in old_subnet['ports']:
                # newly added port
                self._add_flows_to_port(other_gateway_macs, port,
                                        subnet['seg_id'])
                self._add_flows_to_hosted_port(port, gateway_mac)
                continue

            # known port
            self._del_flows_to_port(
                old_gateway_macs - gateway_macs, port)
            self._add_flows_to_port(
                gateway_macs - old_gateway_macs, port, subnet['seg_id'])

            old_port = old_subnet['ports'].pop(port_id)
            if self._port_moved_out_of_host(old_port, port):
                self._del_flows_to_hosted_port(old_port)
            elif self._port_moved_into_host(old_port, port):
                self._add_flows_to_hosted_port(port, gateway_mac)

        for port in old_subnet['ports'].values():
            self._del_flows_to_port(other_gateway_macs, port)
            self._del_flows_to_hosted_port(port)

    def _subnet_deleted(self, subnet, cidrs, gateway_macs):
        gateway_mac = subnet['gateway_mac']
        other_cidrs = cidrs - set([subnet['cidr']])
        other_gateway_macs = gateway_macs - set([gateway_mac])

        self._del_flows_to_other_subnets(gateway_mac, other_cidrs)
        for port in subnet['ports'].values():
            self._del_flows_to_port(other_gateway_macs, port)
            self._del_flows_to_hosted_port(port)

    def _dvr_added(self, dvr_id, new_dvr):
        self.registered_routers[dvr_id] = new_dvr
        cidrs = set(
            subnet['cidr'] for subnet in new_dvr.values())
        gateway_macs = set(
            subnet['gateway_mac'] for subnet in new_dvr.values())

        for subnet_id, subnet in new_dvr.iteritems():
            self._subnet_added(subnet_id, subnet, cidrs, gateway_macs)

    def _port_moved_into_host(self, old_port, new_port):
        current_host = new_port['host']
        last_host = old_port['host']
        return current_host == self.host and current_host != last_host

    def _port_moved_out_of_host(self, old_port, new_port):
        current_host = new_port['host']
        last_host = old_port['host']
        return last_host == self.host and current_host != last_host

    def _dvr_updated(self, dvr_id, new_dvr):
        old_dvr = self.registered_routers.pop(dvr_id)
        self.registered_routers[dvr_id] = new_dvr
        old_cidrs = set(
            subnet['cidr'] for subnet in old_dvr.values())
        old_gateway_macs = set(
            subnet['gateway_mac'] for subnet in old_dvr.values())
        cidrs = set(
            subnet['cidr'] for subnet in new_dvr.values())
        gateway_macs = set(
            subnet['gateway_mac'] for subnet in new_dvr.values())

        for subnet_id, subnet in new_dvr.iteritems():
            if subnet_id not in old_dvr:
                # newly added subnet
                self._subnet_added(subnet_id, subnet, cidrs, gateway_macs)
            else:
                old_subnet = old_dvr.pop(subnet_id)
                self._subnet_updated(
                    subnet_id, (old_subnet, subnet),
                    (old_cidrs, cidrs), (old_gateway_macs, gateway_macs))

        for subnet in old_dvr.values():
            self._subnet_deleted(subnet, old_cidrs, old_gateway_macs)

    def _dvr_deleted(self, dvr_id):
        deleted_dvr = self.registered_routers.pop(dvr_id)
        cidrs = set(
            subnet['cidr'] for subnet in deleted_dvr.values())
        gateway_macs = set(
            subnet['gateway_mac'] for subnet in deleted_dvr.values())

        for subnet in deleted_dvr.values():
            self._subnet_deleted(subnet, cidrs, gateway_macs)

    def sync_dvr_ports(self, phy_brs):
        if not self.dvr_mac_address:
            self._get_dvr_mac_address()
            self._set_src_to_dvr()
            self.setup_dvr_flows_on_phys_brs(phy_brs)

        if time.time() - self.last_sync < self.sync_interval:
            return

        LOG.debug("Started OpenFlow EW DVR sync_dvr_ports()")

        try:
            sync_dvrs = self.plugin_rpc.get_openflow_ew_dvrs(
                self.context, self.host)
        except Exception:
            LOG.exception("Error syncing dvr ports")
            return
        LOG.debug("L2 Agent OF-EW DVR: Received response for "
                  "get_openflow_ew_dvrs() from plugin: %r", sync_dvrs)

        dvrs_to_delete = set()
        dvrs_to_update = set()
        for dvr_id in self.registered_routers:
            if dvr_id not in sync_dvrs:
                dvrs_to_delete.add(dvr_id)
            else:
                dvrs_to_update.add(dvr_id)

        for dvr_id in dvrs_to_delete:
            # handle deleted dvrs
            self._dvr_deleted(dvr_id)

        for dvr_id in dvrs_to_update:
            # router is known, check update
            new_dvr = sync_dvrs.pop(dvr_id)
            self._dvr_updated(dvr_id, new_dvr)

        # Handle newly added dvrs
        for dvr_id, new_dvr in sync_dvrs.iteritems():
            self._dvr_added(dvr_id, new_dvr)

        self.last_sync = time.time()

        LOG.debug("OpenFlow EW DVR sync_dvr_ports() finished, register "
                  "routers are: %r", self.registered_routers)

    # Following functions are kept only for being the same with
    # ovs_dvr_neutron_agent.OVSDVRNeutronAgent.
    # TODO: should better define a base class for different implementations
    # of DVR agents.

    def reset_ovs_parameters(self, integ_br, tun_br,
                             patch_int_ofport, patch_tun_ofport):
        pass

    def process_tunneled_network(self, network_type, lvid, segmentation_id):
        pass

    def bind_port_to_dvr(self, port, network_type, fixed_ips,
                         device_owner, local_vlan_id):
        pass

    def unbind_port_from_dvr(self, vif_port, local_vlan_id):
        pass
