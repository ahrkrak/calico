# -*- coding: utf-8 -*-
# Copyright (c) 2015 Metaswitch Networks
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
"""
felix.endpoint
~~~~~~~~~~~~~

Endpoint management.
"""
import logging
from subprocess import CalledProcessError
from calico.felix import devices, futils
from calico.felix.actor import actor_event
from calico.felix.futils import FailedSystemCall
from calico.felix.futils import IPV4
from calico.felix.refcount import ReferenceManager, RefCountedActor
from calico.felix.dispatch import DispatchChains
from calico.felix.profilerules import RulesManager
from calico.felix.frules import (CHAIN_TO_PREFIX, profile_to_chain_name,
                                 CHAIN_FROM_PREFIX, commented_drop_fragment)

_log = logging.getLogger(__name__)


class EndpointManager(ReferenceManager):
    def __init__(self, config, ip_type,
                 iptables_updater,
                 dispatch_chains,
                 rules_manager):
        super(EndpointManager, self).__init__(qualifier=ip_type)

        # Configuration and version to use
        self.config = config
        self.ip_type = ip_type
        self.ip_version = futils.IP_TYPE_TO_VERSION[ip_type]

        # Peers/utility classes.
        self.iptables_updater = iptables_updater
        self.dispatch_chains = dispatch_chains
        self.rules_mgr = rules_manager

        # All endpoint dicts that we know about.
        self.endpoints_by_id = {}
        self.endpoint_id_by_iface_name = {}

        # Set of endpoints that are live on this host.  I.e. ones that we've
        # increffed.
        self.local_endpoint_ids = set()

    def _create(self, object_id):
        """
        Overrides ReferenceManager._create()
        """
        return LocalEndpoint(self.config,
                             object_id,
                             self.ip_type,
                             self.iptables_updater,
                             self.dispatch_chains,
                             self.rules_mgr)

    def _on_object_started(self, endpoint_id, obj):
        """
        Callback from a LocalEndpoint to report that it has started.
        Overrides ReferenceManager._on_object_started
        """
        ep = self.endpoints_by_id.get(endpoint_id)
        obj.on_endpoint_update(ep, async=True)

    @actor_event
    def apply_snapshot(self, endpoints_by_id):
        missing_endpoints = set(self.endpoints_by_id.keys())

        # Tell the dispatch chains about the local endpoints in advance so
        # that we don't flap the dispatch chain at start-of-day.
        local_iface_name_to_ep_id = {}
        for ep_id, ep in endpoints_by_id.iteritems():
            if ep and ep["host"] == self.config.HOSTNAME and ep.get("name"):
                local_iface_name_to_ep_id[ep.get("name")] = ep_id
        self.dispatch_chains.apply_snapshot(local_iface_name_to_ep_id,
                                            async=True)

        for endpoint_id, endpoint in endpoints_by_id.iteritems():
            self.on_endpoint_update(endpoint_id, endpoint)
            missing_endpoints.discard(endpoint_id)
            self._maybe_yield()
        for endpoint_id in missing_endpoints:
            self.on_endpoint_update(endpoint_id, None)
            self._maybe_yield()

    @actor_event
    def on_endpoint_update(self, endpoint_id, endpoint):
        """
        Event to indicate that an endpoint has been updated (including creation
        or deletion).

        :param str endpoint_id: The endpoint ID in question.
        :endpoint dict[str]: Dictionary of all endpoint data. If None, the
                             endpoint is to be deleted.
        """
        if self._is_starting_or_live(endpoint_id):
            # Local endpoint thread is running; tell it of the change.
            self.objects_by_id[endpoint_id].on_endpoint_update(endpoint,
                                                               async=True)
        if endpoint is None:
            # Deletion. Remove from the list.
            _log.info("Endpoint %s deleted", endpoint_id)
            old_ep = self.endpoints_by_id.pop(endpoint_id, {})
            if old_ep.get("name") in self.endpoint_id_by_iface_name:
                self.endpoint_id_by_iface_name.pop(old_ep.get("name"))
            if endpoint_id in self.local_endpoint_ids:
                self.decref(endpoint_id)
                self.local_endpoint_ids.remove(endpoint_id)
        else:
            # Creation or modification
            _log.info("Endpoint %s modified or created", endpoint_id)
            self.endpoints_by_id[endpoint_id] = endpoint
            self.endpoint_id_by_iface_name[endpoint["name"]] = endpoint_id

        if endpoint and endpoint["host"] == self.config.HOSTNAME:
            _log.debug("Endpoint is local, ensuring it is active.")
            if endpoint_id not in self.local_endpoint_ids:
                # This will trigger _on_object_activated to pass the endpoint
                # we just saved off to the endpoint.
                self.local_endpoint_ids.add(endpoint_id)
                self.get_and_incref(endpoint_id)

    @actor_event
    def on_interface_update(self, name):
        """
        Called when an interface is created or changes state.

        The interface may be any interface on the host, not necessarily
        one managed by any endpoint of this server.
        """
        try:
            endpoint_id = self.endpoint_id_by_iface_name[name]
            _log.info("Endpoint %s received interface update for %s",
                      endpoint_id, name)
            if self._is_starting_or_live(endpoint_id):
                # LocalEndpoint is running, so tell it about the change.
                ep = self.objects_by_id[endpoint_id]
                ep.on_interface_update(async=True)

        except KeyError:
            _log.debug("Update on interface %s that we do not care about",
                       name)


class LocalEndpoint(RefCountedActor):

    def __init__(self, config, endpoint_id, ip_type, iptables_updater,
                 dispatch_chains, rules_manager):
        super(LocalEndpoint, self).__init__(qualifier="%s(%s)" %
                                            (endpoint_id, ip_type))
        assert isinstance(dispatch_chains, DispatchChains)
        assert isinstance(rules_manager, RulesManager)

        self.endpoint_id = endpoint_id

        self.config = config
        self.ip_type = ip_type
        self.ip_version = futils.IP_TYPE_TO_VERSION[ip_type]
        self.iptables_updater = iptables_updater
        self.dispatch_chains = dispatch_chains
        self.rules_mgr = rules_manager

        # Will be filled in as we learn about the OS interface and the
        # endpoint config.
        self.endpoint = None
        self._iface_name = None
        self._suffix = None

        # Track whether the last attempt to program the dataplane succeeded.
        # We'll force a reprogram next time we get a kick.
        self._failed = False

    @actor_event
    def on_endpoint_update(self, endpoint):
        """
        Called when this endpoint has received an update.
        :param dict[str] endpoint: endpoint parameter dictionary.
        """
        _log.debug("Endpoint updated: %s", endpoint)
        if endpoint and not self.endpoint:
            # This is the first time we have seen the endpoint, so extract the
            # interface name and endpoint ID.
            self._iface_name = endpoint["name"]
            self._suffix = interface_to_suffix(self.config,
                                               self._iface_name)
        was_ready = self._ready

        old_profile_id = self.endpoint and self.endpoint["profile_id"]
        new_profile_id = endpoint and endpoint["profile_id"]
        if old_profile_id != new_profile_id:
            if old_profile_id:
                # Clean up the old profile.
                self.rules_mgr.decref(old_profile_id, async=True)
            if new_profile_id is not None:
                _log.debug("Acquiring new profile %s", new_profile_id)
                self.rules_mgr.get_and_incref(new_profile_id, async=True)

        # Store off the endpoint we were passed.
        self.endpoint = endpoint

        if endpoint:
            # Configure the network interface; may fail if not there yet (in
            # which case we'll just do it when the interface comes up).
            self._configure_interface()
        else:
            # Remove the network programming.
            self._deconfigure_interface()

        self._maybe_update(was_ready)
        _log.debug("%s finished processing update", self)

    @actor_event
    def on_unreferenced(self):
        """
        Overrides RefCountedActor:on_unreferenced.
        """
        _log.info("%s now unreferenced, cleaning up", self)
        assert not self._ready, "Should be deleted before being unreffed."
        self._notify_cleanup_complete()

    @actor_event
    def on_interface_update(self):
        """
        Actor event to report that the interface is either up or changed.
        """
        _log.info("Endpoint %s received interface kick", self.endpoint_id)
        self._configure_interface()

    @property
    def _missing_deps(self):
        """
        Returns a list of missing dependencies.
        """
        missing_deps = []
        if not self.endpoint:
            missing_deps.append("endpoint")
        elif self.endpoint.get("state", "active") != "active":
            missing_deps.append("endpoint active")
        elif not self.endpoint.get("profile_id"):
            missing_deps.append("profile")
        return missing_deps

    @property
    def _ready(self):
        """
        Returns whether this LocalEndpoint has any dependencies preventing it
        programming its rules.
        """
        return not self._missing_deps

    def _maybe_update(self, was_ready):
        """
        Update the relevant programming for this endpoint.

        :param bool was_ready: Whether this endpoint has already been
                               successfully configured.
        """
        is_ready = self._ready
        if not is_ready:
            _log.debug("%s not ready, waiting on %s", self, self._missing_deps)
        if self._failed or is_ready != was_ready:
            ifce_name = self._iface_name
            if is_ready:
                # We've got all the info and everything is active.
                if self._failed:
                    _log.warn("Retrying programming after a failure")
                self._failed = False  # Ready to try again...
                _log.info("%s became ready to program.", self)
                self._update_chains()
                self.dispatch_chains.on_endpoint_added(
                    self._iface_name, self.endpoint_id, async=True)
            else:
                # We were active but now we're not, withdraw the dispatch rule
                # and our chain.  We must do this to allow iptables to remove
                # the profile chain.
                _log.info("%s became unready.", self)
                self._failed = False  # Don't care any more.

                self.dispatch_chains.on_endpoint_removed(ifce_name,
                                                         async=True)
                self._remove_chains()

    def _update_chains(self):
        updates, deps = _get_endpoint_rules(
            self._suffix,
            self._iface_name,
            self.ip_version,
            self.endpoint.get("ipv%s_nets" % self.ip_version, []),
            self.endpoint["mac"],
            self.endpoint["profile_id"])
        try:
            self.iptables_updater.rewrite_chains(updates, deps, async=False)
        except CalledProcessError:
            _log.exception("Failed to program chains for %s. Removing.", self)
            self._failed = True
            self._remove_chains()

    def _remove_chains(self):
        try:
            self.iptables_updater.delete_chains(chain_names(self._suffix),
                                                async=True)
        except CalledProcessError:
            _log.exception("Failed to delete chains for %s", self)
            self._failed = True

    def _configure_interface(self):
        """
        Applies sysctls and routes to the interface.
        """
        try:
            if self.ip_type == IPV4:
                devices.configure_interface_ipv4(self._iface_name)
                nets_key = "ipv4_nets"
            else:
                ipv6_gw = self.endpoint.get("ipv6_gateway", None)
                devices.configure_interface_ipv6(self._iface_name, ipv6_gw)
                nets_key = "ipv6_nets"

            ips = set()
            for ip in self.endpoint.get(nets_key, []):
                ips.add(futils.net_to_ip(ip))
            devices.set_routes(self.ip_type, ips,
                               self._iface_name,
                               self.endpoint["mac"])

        except (IOError, FailedSystemCall, CalledProcessError):
            if not devices.interface_exists(self._iface_name):
                _log.info("Interface %s for %s does not exist yet",
                           self._iface_name, self.endpoint_id)
            elif not devices.interface_up(self._iface_name):
                _log.info("Interface %s for %s is not up yet",
                           self._iface_name, self.endpoint_id)
            else:
                # OK, that really should not happen.
                _log.exception("Failed to configure interface %s for %s",
                               self._iface_name, self.endpoint_id)
                raise

    def _deconfigure_interface(self):
        """
        Removes routes from the interface.
        """
        try:
            devices.set_routes(self.ip_type, set(), self._iface_name, None)

        except (IOError, FailedSystemCall, CalledProcessError):
            if not devices.interface_exists(self._iface_name):
                # Deleted under our feet - so the rules are gone.
                _log.debug("Interface %s for %s deleted",
                           self._iface_name, self.endpoint_id)
            else:
                # An error deleting the rules. Log and continue.
                _log.exception("Cannot delete rules for interface %s for %s",
                               self._iface_name, self.endpoint_id)

    def __str__(self):
        return ("Endpoint<%s,id=%s,iface=%s>" %
                (self.ip_type, self.endpoint_id,
                 self._iface_name or "unknown"))


def interface_to_suffix(config, iface_name):
    suffix = iface_name.replace(config.IFACE_PREFIX, "", 1)
    # The suffix is surely not very long, but make sure.
    suffix = futils.uniquely_shorten(suffix, 16)
    return suffix


def chain_names(endpoint_suffix):
    to_chain_name = (CHAIN_TO_PREFIX + endpoint_suffix)
    from_chain_name = (CHAIN_FROM_PREFIX + endpoint_suffix)
    return to_chain_name, from_chain_name


def _get_endpoint_rules(suffix, iface, ip_version, local_ips, mac, profile_id):
    to_chain_name, from_chain_name = chain_names(suffix)

    to_chain = ["--flush %s" % to_chain_name]
    if ip_version == 6:
        #  In ipv6 only, there are 6 rules that need to be created first.
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 130
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 131
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmptype 132
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmp router-advertisement
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmp neighbour-solicitation
        #  RETURN ipv6-icmp anywhere anywhere ipv6-icmp neighbour-advertisement
        #
        #  These rules are ICMP types 130, 131, 132, 134, 135 and 136, and can
        #  be created on the command line with something like :
        #     ip6tables -A plw -j RETURN --protocol ipv6-icmp --icmpv6-type 130
        for icmp_type in ["130", "131", "132", "134", "135", "136"]:
            to_chain.append("--append %s --jump RETURN "
                            "--protocol ipv6-icmp "
                            "--icmpv6-type %s" % (to_chain_name, icmp_type))
    to_chain.append("--append %s --match conntrack --ctstate INVALID "
                    "--jump DROP" % to_chain_name)
    to_chain.append("--append %s --match conntrack "
                    "--ctstate RELATED,ESTABLISHED --jump RETURN" %
                    to_chain_name)
    assert profile_id, "Profile ID should be set, not %s" % profile_id
    profile_in_chain = profile_to_chain_name("inbound", profile_id)
    to_chain.append("--append %s --goto %s" %
                    (to_chain_name, profile_in_chain))
    to_deps = set([profile_in_chain])

    # Now the chain that manages packets from the interface...
    from_chain = ["--flush %s" % from_chain_name]
    if ip_version == 6:
        # In ipv6 only, allows all ICMP traffic from this endpoint to anywhere.
        from_chain.append("--append %s --protocol ipv6-icmp" % from_chain_name)

    # Conntrack rules.
    from_chain.append("--append %s --match conntrack --ctstate INVALID "
                      "--jump DROP" % from_chain_name)
    from_chain.append("--append %s --match conntrack "
                      "--ctstate RELATED,ESTABLISHED --jump RETURN" %
                      from_chain_name)

    if ip_version == 4:
        from_chain.append("--append %s --protocol udp --sport 68 --dport 67 "
                          "--jump RETURN" % from_chain_name)
    else:
        assert ip_version == 6
        from_chain.append("--append %s --protocol udp --sport 546 --dport 547 "
                          "--jump RETURN" % from_chain_name)

    # Anti-spoofing rules.  Only allow traffic from known (IP, MAC) pairs to
    # get to the profile chain, drop other traffic.
    profile_out_chain = profile_to_chain_name("outbound", profile_id)
    from_deps = set([profile_out_chain])
    for ip in local_ips:
        if "/" in ip:
            cidr = ip
        else:
            cidr = "%s/32" % ip if ip_version == 4 else "%s/128" % ip
        # Note use of --goto rather than --jump; this means that when the
        # profile chain returns, it will return the chain that called us, not
        # this chain.
        from_chain.append("--append %s --src %s --match mac --mac-source %s "
                          "--goto %s" % (from_chain_name, cidr,
                                         mac.upper(), profile_out_chain))
    from_chain.append(commented_drop_fragment(from_chain_name,
                                              "Anti-spoof DROP:"))

    updates = {to_chain_name: to_chain, from_chain_name: from_chain}
    deps = {to_chain_name: to_deps, from_chain_name: from_deps}
    return updates, deps
