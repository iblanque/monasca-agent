#!/bin/env python

# (c) Copyright 2014-2016 Hewlett Packard Enterprise Development Company LP
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
"""Monasca Agent interface for libvirt metrics"""

import json
import libvirt
import math
import os
import stat
import subprocess
import time

from calendar import timegm
from copy import deepcopy
from datetime import datetime
from datetime import timedelta
from monasca_agent.collector.checks import AgentCheck
from monasca_agent.collector.virt import inspector
from netaddr import all_matching_cidrs

DOM_STATES = {libvirt.VIR_DOMAIN_BLOCKED: 'VM is blocked',
              libvirt.VIR_DOMAIN_CRASHED: 'VM has crashed',
              libvirt.VIR_DOMAIN_NONE: 'VM has no state',
              libvirt.VIR_DOMAIN_PAUSED: 'VM is paused',
              libvirt.VIR_DOMAIN_PMSUSPENDED: 'VM is in power management (s3) suspend',
              libvirt.VIR_DOMAIN_SHUTDOWN: 'VM is shutting down',
              libvirt.VIR_DOMAIN_SHUTOFF: 'VM has been shut off'}


class LibvirtCheck(AgentCheck):

    """Inherit Agent class and gather libvirt metrics"""

    def __init__(self, name, init_config, agent_config, instances=None):
        AgentCheck.__init__(self, name, init_config, agent_config, instances=[{}])
        self.instance_cache_file = "{0}/{1}".format(self.init_config.get('cache_dir'),
                                                    'libvirt_instances.json')
        self.metric_cache_file = "{0}/{1}".format(self.init_config.get('cache_dir'),
                                                  'libvirt_metrics.json')
        self.use_bits = self.init_config.get('network_use_bits')
        if self.init_config.get('disk_collection_period'):
            self._disk_collection_period = int(self.init_config.get('disk_collection_period'))
            self._last_disk_collect_time = datetime.fromordinal(1)
        else:
            self._disk_collection_period = 0
        self._skip_disk_collection = False

    def _test_vm_probation(self, created):
        """Test to see if a VM was created within the probation period.

        Convert an ISO-8601 timestamp into UNIX epoch timestamp from now
        and compare that against configured vm_probation.  Return the
        number of seconds this VM will remain in probation.
        """
        dt = datetime.strptime(created, '%Y-%m-%dT%H:%M:%SZ')
        created_sec = (time.time() - timegm(dt.timetuple()))
        probation_time = self.init_config.get('vm_probation', 300) - created_sec
        return int(probation_time)

    def _get_metric_name(self, orig_name):
        # Rename "tx" to "out" and "rx" to "in"
        metric_name = orig_name.replace("tx", "out").replace("rx", "in")
        if self.use_bits:
            metric_name = metric_name.replace("bytes", "bits")
        return metric_name

    @staticmethod
    def _get_metric_rate_name(metric_name):
        """Change the metric name to a rate, i.e. "net.rx_bytes"
        gets converted to "net.rx_bytes_sec"
        """
        return "{0}_sec".format(metric_name)

    @staticmethod
    def _validate_secgroup(cache, instance, source_ip):
        """Search through an instance's security groups for pingability
        """
        for instance_secgroup in instance.security_groups:
            for secgroup in cache:
                if ((secgroup['tenant_id'] == instance.tenant_id and
                     secgroup['name'] == instance_secgroup['name'])):
                    for rule in secgroup['security_group_rules']:
                        if rule['protocol'] == 'icmp':
                            if ((not rule['remote_ip_prefix'] or
                                 all_matching_cidrs(source_ip,
                                                    [rule['remote_ip_prefix']]))):
                                return True

    def _update_instance_cache(self):
        """Collect instance_id, project_id, and AZ for all instance UUIDs
        """
        from novaclient import client

        id_cache = {}
        flavor_cache = {}
        port_cache = None
        netns = None
        # Get a list of all instances from the Nova API
        nova_client = client.Client(2, self.init_config.get('admin_user'),
                                    self.init_config.get('admin_password'),
                                    self.init_config.get('admin_tenant_name'),
                                    self.init_config.get('identity_uri'),
                                    endpoint_type='internalURL',
                                    service_type="compute",
                                    region_name=self.init_config.get('region_name'))
        instances = nova_client.servers.list(search_opts={'all_tenants': 1,
                                                          'host': self.hostname})
        # Lay the groundwork for fetching VM IPs and network namespaces
        if self.init_config.get('ping_check'):
            from neutronclient.v2_0 import client
            nu = client.Client(username=self.init_config.get('admin_user'),
                               password=self.init_config.get('admin_password'),
                               tenant_name=self.init_config.get('admin_tenant_name'),
                               auth_url=self.init_config.get('identity_uri'),
                               endpoint_type='internalURL')
            port_cache = nu.list_ports()['ports']
            # Finding existing network namespaces is an indication that either
            # DVR agent_mode is enabled, or this is all-in-one (like devstack)
            netns = subprocess.check_output(['ip', 'netns', 'list'])
            if netns == '':
                self.log.warn("Unable to ping VMs, no network namespaces found." +
                              "Either no VMs are present, or routing is centralized.")

        for instance in instances:
            instance_ports = []
            inst_name = instance.__getattr__('OS-EXT-SRV-ATTR:instance_name')
            inst_az = instance.__getattr__('OS-EXT-AZ:availability_zone')
            if instance.flavor['id'] in flavor_cache:
                inst_flavor = flavor_cache[instance.flavor['id']]
            else:
                inst_flavor = nova_client.flavors.get(instance.flavor['id'])
                flavor_cache[instance.flavor['id']] = inst_flavor
            if port_cache:
                instance_ports = [p['id'] for p in port_cache if p['device_id'] == instance.id]
            id_cache[inst_name] = {'instance_uuid': instance.id,
                                   'hostname': instance.name,
                                   'zone': inst_az,
                                   'created': instance.created,
                                   'tenant_id': instance.tenant_id,
                                   'vcpus': inst_flavor.vcpus,
                                   'ram': inst_flavor.ram,
                                   'disk': inst_flavor.disk,
                                   'instance_ports': instance_ports}

            for config_var in ['metadata', 'customer_metadata']:
                if self.init_config.get(config_var):
                    for metadata in self.init_config.get(config_var):
                        if instance.metadata.get(metadata):
                            id_cache[inst_name][metadata] = (instance.metadata.
                                                             get(metadata))

            # Build a list of pingable IP addresses attached to this VM and the
            # appropriate namespace, for use in ping tests
            if netns:
                secgroup_cache = nu.list_security_groups()['security_groups']
                self._build_ip_list(instance, inst_name,
                                    secgroup_cache, port_cache, id_cache)

        id_cache['last_update'] = int(time.time())

        # Write the updated cache
        try:
            with open(self.instance_cache_file, 'w') as cache_json:
                json.dump(id_cache, cache_json)
            if stat.S_IMODE(os.stat(self.instance_cache_file).st_mode) != 0o600:
                os.chmod(self.instance_cache_file, 0o600)
        except IOError as e:
            self.log.error("Cannot write to {0}: {1}".format(self.instance_cache_file, e))

        return id_cache

    def _build_ip_list(self, instance, inst_name, secgroup_cache, port_cache, id_cache):
        # Find all active fixed IPs for this VM, fetch each subnet_id
        for net in instance.addresses:
            for ip in instance.addresses[net]:
                if ip['OS-EXT-IPS:type'] == 'fixed' and ip['version'] == 4:
                    subnet_id = None
                    nsuuid = None
                    for port in port_cache:
                        if ((port['mac_address'] == ip['OS-EXT-IPS-MAC:mac_addr'] and
                             port['tenant_id'] == instance.tenant_id and
                             port['status'] == 'ACTIVE')):
                            for fixed in port['fixed_ips']:
                                if fixed['ip_address'] == ip['addr']:
                                    subnet_id = fixed['subnet_id']
                                    break
                    # Use the subnet_id to find the router
                    ping_allowed = False
                    if subnet_id is not None:
                        for port in port_cache:
                            if ((port['device_owner'].startswith('network:router_interface') and
                                 port['tenant_id'] == instance.tenant_id and
                                 port['status'] == 'ACTIVE')):
                                nsuuid = port['device_id']
                                for fixed in port['fixed_ips']:
                                    if fixed['subnet_id'] == subnet_id:
                                        # Validate security group
                                        if self._validate_secgroup(secgroup_cache,
                                                                   instance,
                                                                   fixed['ip_address']):
                                            ping_allowed = True
                                            break
                            if nsuuid is not None:
                                break
                    if nsuuid is not None and ping_allowed:
                        if 'network' not in id_cache[inst_name]:
                            id_cache[inst_name]['network'] = []
                        id_cache[inst_name]['network'].append({'namespace': "qrouter-{0}".format(nsuuid),
                                                               'ip': ip['addr']})
                    elif ping_allowed is False:
                        self.log.debug("ICMP disallowed for {0} on {1}".format(inst_name,
                                                                               ip['addr']))

    def _load_instance_cache(self):
        """Load the cache map of instance names to Nova data.
           If the cache does not yet exist or is damaged, (re-)build it.
        """
        instance_cache = {}
        try:
            with open(self.instance_cache_file, 'r') as cache_json:
                instance_cache = json.load(cache_json)

                # Is it time to force a refresh of this data?
                if self.init_config.get('nova_refresh') is not None:
                    time_diff = time.time() - instance_cache['last_update']
                    if time_diff > self.init_config.get('nova_refresh'):
                        self._update_instance_cache()
        except (IOError, TypeError, ValueError):
            # The file may not exist yet, or is corrupt.  Rebuild it now.
            self.log.warning("Instance cache missing or corrupt, rebuilding.")
            instance_cache = self._update_instance_cache()
            pass

        return instance_cache

    def _load_metric_cache(self):
        """Load the counter metrics from the previous collection iteration
        """
        metric_cache = {}
        try:
            with open(self.metric_cache_file, 'r') as cache_json:
                metric_cache = json.load(cache_json)
        except (IOError, TypeError, ValueError):
            # The file may not exist yet.
            self.log.warning("Metrics cache missing or corrupt, rebuilding.")
            metric_cache = {}
            pass

        return metric_cache

    def _update_metric_cache(self, metric_cache, run_time):
        # Remove inactive VMs from the metric cache
        write_metric_cache = deepcopy(metric_cache)
        for instance in metric_cache:
            if (('cpu.time' not in metric_cache[instance] or
                 self._test_vm_probation(time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                         time.gmtime(metric_cache[instance]['cpu.time']['timestamp'] + run_time))) < 0)):
                self.log.info("Expiring old/empty {0} from cache".format(instance))
                del(write_metric_cache[instance])
        try:
            with open(self.metric_cache_file, 'w') as cache_json:
                json.dump(write_metric_cache, cache_json)
            if stat.S_IMODE(os.stat(self.metric_cache_file).st_mode) != 0o600:
                os.chmod(self.metric_cache_file, 0o600)
        except IOError as e:
            self.log.error("Cannot write to {0}: {1}".format(self.metric_cache_file, e))

    def _inspect_network(self, insp, inst, inst_name, instance_cache, metric_cache, dims_customer, dims_operations):
        """Inspect network metrics for an instance"""
        for vnic in insp.inspect_vnics(inst):
            sample_time = time.time()
            vnic_dimensions = {'device': vnic[0].name}
            instance_ports = instance_cache.get(inst_name)['instance_ports']
            partial_port_id = vnic[0].name.split('tap')[1]
            # Multiple networked guest
            for port in instance_ports:
                if partial_port_id == port[:11]:
                    vnic_dimensions['port_id'] = port
                    break
            for metric in vnic[1]._fields:
                metric_name = "net.{0}".format(metric)
                if metric_name not in metric_cache[inst_name]:
                    metric_cache[inst_name][metric_name] = {}

                value = int(vnic[1].__getattribute__(metric))
                if vnic[0].name in metric_cache[inst_name][metric_name]:
                    last_update_time = metric_cache[inst_name][metric_name][vnic[0].name]['timestamp']
                    time_diff = sample_time - float(last_update_time)
                    rate_value = self._calculate_rate(value,
                                                      metric_cache[inst_name][metric_name][vnic[0].name]['value'],
                                                      time_diff)
                    if rate_value < 0:
                        # Bad value, save current reading and skip
                        self.log.warn("Ignoring negative network sample for: "
                                      "{0} new value: {1} old value: {2}"
                                      .format(inst_name, value,
                                              metric_cache[inst_name][metric_name][vnic[0].name]['value']))
                        metric_cache[inst_name][metric_name][vnic[0].name] = {
                            'timestamp': sample_time,
                            'value': value}
                        continue
                    rate_name = self._get_metric_rate_name(metric_name)
                    rate_name = self._get_metric_name(rate_name)
                    if self.use_bits:
                        rate_value *= 8
                    # Customer
                    this_dimensions = vnic_dimensions.copy()
                    this_dimensions.update(dims_customer)
                    self.gauge(rate_name, rate_value,
                               dimensions=this_dimensions,
                               delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                               hostname=instance_cache.get(inst_name)['hostname'])
                    # Operations (metric name prefixed with "vm."
                    this_dimensions = vnic_dimensions.copy()
                    this_dimensions.update(dims_operations)
                    self.gauge("vm.{0}".format(rate_name), rate_value,
                               dimensions=this_dimensions)
                # Report raw counters.
                mapped_name = self._get_metric_name(metric_name)
                weighted_value = value
                if self.use_bits:
                    weighted_value = value * 8
                # Customer
                this_dimensions = vnic_dimensions.copy()
                this_dimensions.update(dims_customer)
                self.gauge(mapped_name, weighted_value,
                           dimensions=this_dimensions,
                           delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                           hostname=instance_cache.get(inst_name)['hostname'])
                # Operations (metric name prefixed with "vm.")
                this_dimensions = vnic_dimensions.copy()
                this_dimensions.update(dims_operations)
                self.gauge("vm.{0}".format(mapped_name),
                           weighted_value, dimensions=this_dimensions)
                # Save this metric to the cache
                metric_cache[inst_name][metric_name][vnic[0].name] = {
                    'timestamp': sample_time,
                    'value': value}

    def _inspect_cpu(self, insp, inst, inst_name, instance_cache, metric_cache, dims_customer, dims_operations):
        """Inspect cpu metrics for an instance"""

        sample_time = float("{:9f}".format(time.time()))
        if 'cpu.time' in metric_cache[inst_name]:
            cpu_info = insp.inspect_cpus(inst)
            # I have a prior value, so calculate the raw_perc & push the metric
            cpu_diff = cpu_info.time - metric_cache[inst_name]['cpu.time']['value']
            time_diff = sample_time - float(metric_cache[inst_name]['cpu.time']['timestamp'])
            # Convert time_diff to nanoseconds, and calculate percentage
            raw_perc = (cpu_diff / (time_diff * 1000000000)) * 100
            # Divide by the number of cores to normalize the percentage
            normalized_perc = (raw_perc / cpu_info.number)
            if raw_perc < 0:
                # Bad value, save current reading and skip
                self.log.warn("Ignoring negative CPU sample for: "
                              "{0} new cpu time: {1} old cpu time: {2}"
                              .format(inst_name, cpu_info.time,
                                      metric_cache[inst_name]['cpu.time']['value']))
                metric_cache[inst_name]['cpu.time'] = {'timestamp': sample_time,
                                                       'value': cpu_info.time}
                return

            self.gauge('cpu.utilization_perc', int(round(raw_perc, 0)),
                       dimensions=dims_customer,
                       delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                       hostname=instance_cache.get(inst_name)['hostname'])
            self.gauge('cpu.utilization_norm_perc', int(round(normalized_perc, 0)),
                       dimensions=dims_customer,
                       delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                       hostname=instance_cache.get(inst_name)['hostname'])
            self.gauge('vm.cpu.utilization_perc', int(round(raw_perc, 0)),
                       dimensions=dims_operations)
            self.gauge('vm.cpu.utilization_norm_perc', int(round(normalized_perc, 0)),
                       dimensions=dims_operations)
            self.gauge('vm.cpu.time_ns', cpu_info.time,
                       dimensions=dims_operations)

        metric_cache[inst_name]['cpu.time'] = {'timestamp': sample_time,
                                               'value': cpu_info.time}

    def _inspect_disks(self, insp, inst, inst_name, instance_cache, metric_cache, dims_customer, dims_operations):
        """Inspect disk metrics for an instance"""

        metric_aggregate = {}
        for disk in insp.inspect_disks(inst):
            sample_time = time.time()
            disk_dimensions = {'device': disk[0].device}
            for metric in disk[1]._fields:
                metric_name = "io.{0}".format(metric.replace('requests', 'ops'))
                if metric_name not in metric_cache[inst_name]:
                    metric_cache[inst_name][metric_name] = {}

                value = int(disk[1].__getattribute__(metric))
                metric_aggregate[metric_name] = metric_aggregate.get(
                    metric_name, 0) + value
                if disk[0].device in metric_cache[inst_name][metric_name]:
                    cached_val = metric_cache[inst_name][metric_name][disk[
                        0].device]['value']
                    last_update_time = metric_cache[inst_name][metric_name][disk[
                        0].device]['timestamp']
                    time_diff = sample_time - float(last_update_time)
                    rate_value = self._calculate_rate(value, cached_val, time_diff)
                    if rate_value < 0:
                        # Bad value, save current reading and skip
                        self.log.warn("Ignoring negative disk sample for: "
                                      "{0} new value: {1} old value: {2}"
                                      .format(inst_name, value, cached_val))
                        metric_cache[inst_name][metric_name][disk[0].device] = {
                            'timestamp': sample_time,
                            'value': value}
                        continue
                    # Change the metric name to a rate, ie. "io.read_requests"
                    # gets converted to "io.read_ops_sec"
                    rate_name = "{0}_sec".format(metric_name.replace('requests', 'ops'))
                    # Customer
                    this_dimensions = disk_dimensions.copy()
                    this_dimensions.update(dims_customer)
                    self.gauge(rate_name, rate_value, dimensions=this_dimensions,
                               delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                               hostname=instance_cache.get(inst_name)['hostname'])
                    # Operations (metric name prefixed with "vm."
                    this_dimensions = disk_dimensions.copy()
                    this_dimensions.update(dims_operations)
                    self.gauge("vm.{0}".format(rate_name), rate_value,
                               dimensions=this_dimensions)
                    self.gauge("vm.{0}".format(metric_name), value,
                               dimensions=this_dimensions)
                # Save this metric to the cache
                metric_cache[inst_name][metric_name][disk[0].device] = {
                    'timestamp': sample_time,
                    'value': value}

        this_dimensions = dict()
        this_dimensions.update(dims_customer)
        this_dimensions.update(dims_operations)
        for metric in metric_aggregate:
            sample_time = time.time()
            rate_name = "vm.{0}_total_sec".format(metric)
            if rate_name not in metric_cache[inst_name]:
                metric_cache[inst_name][rate_name] = {}
            else:
                last_update_time = metric_cache[inst_name][
                    rate_name]['timestamp']
                time_diff = sample_time - float(last_update_time)
                rate_value = self._calculate_rate(metric_aggregate[metric],
                                                  metric_cache[inst_name][rate_name]['value'],
                                                  time_diff)
                if rate_value < 0:
                    # Bad value, save current reading and skip
                    self.log.warn("Ignoring negative disk sample for: "
                                  "{0} new value: {1} old value: {2}"
                                  .format(inst_name, metric_aggregate[metric],
                                          metric_cache[inst_name][rate_name][
                                              'value']))
                    metric_cache[inst_name][rate_name] = {
                        'timestamp': sample_time,
                        'value': metric_aggregate[metric]}
                    continue
                self.gauge(rate_name, rate_value,
                           dimensions=this_dimensions)
            self.gauge("vm.{0}_total".format(metric), metric_aggregate[
                metric], dimensions=this_dimensions)
            # Save this metric to the cache
            metric_cache[inst_name][rate_name] = {
                'timestamp': sample_time,
                'value': metric_aggregate[metric]}

    def _inspect_disk_info(self, insp, inst, inst_name, instance_cache, metric_cache,
                           dims_customer, dims_operations):
        """Inspect disk metrics for an instance"""

        metric_aggregate = {}
        for disk in insp.inspect_disk_info(inst):
            disk_dimensions = {'device': disk[0].device}
            for metric in disk[1]._fields:
                metric_name = "disk.{0}".format(metric)
                value = int(disk[1].__getattribute__(metric))
                metric_aggregate[metric_name] = metric_aggregate.get(
                    metric_name, 0) + value
                this_dimensions = disk_dimensions.copy()
                this_dimensions.update(dims_customer)
                self.gauge(metric_name, value, dimensions=this_dimensions,
                           delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                           hostname=instance_cache.get(inst_name)['hostname'])
                # Operations (metric name prefixed with "vm."
                this_dimensions = disk_dimensions.copy()
                this_dimensions.update(dims_operations)
                self.gauge("vm.{0}".format(metric_name), value,
                           dimensions=this_dimensions)

        this_dimensions = dict()
        this_dimensions.update(dims_customer)
        this_dimensions.update(dims_operations)
        for metric in metric_aggregate:
            self.gauge("vm.{0}_total".format(metric), metric_aggregate[
                metric], dimensions=this_dimensions)

    def _inspect_state(self, insp, inst, inst_name, instance_cache, dims_customer, dims_operations):
        """Look at the state of the instance, publish a metric using a
           user-friendly description in the 'detail' metadata, and return
           a status code (calibrated to UNIX status codes where 0 is OK)
           so that remaining metrics can be skipped if the VM is not OK
        """
        inst_state = inst.state()
        dom_status = inst_state[0] - 1
        metatag = None

        if inst_state[0] in DOM_STATES:
            metatag = {'detail': DOM_STATES[inst_state[0]]}
        # A nova-suspended VM has a SHUTOFF Power State, but alternate Status
        if inst_state == [libvirt.VIR_DOMAIN_SHUTOFF, 5]:
            metatag = {'detail': 'VM has been suspended'}

        self.gauge('host_alive_status', dom_status, dimensions=dims_customer,
                   delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                   hostname=instance_cache.get(inst_name)['hostname'],
                   value_meta=metatag)
        self.gauge('vm.host_alive_status', dom_status,
                   dimensions=dims_operations,
                   value_meta=metatag)

        return dom_status

    def prepare_run(self):
        """Check if it is time for the disk measurements to be collected"""
        # A separate disk collection period is optional
        if self._disk_collection_period <= 0:
            return

        time_since_last = datetime.now() - self._last_disk_collect_time
        # Handle times that are really close to the disk collection period
        period_with_fudge_factor = timedelta(0, self._disk_collection_period - 1,
                                             500000)
        if time_since_last < period_with_fudge_factor:
            self.log.debug('Skipping disk collection for %d seconds' %
                           (self._disk_collection_period - time_since_last.seconds))
            self._skip_disk_collection = True
        else:
            self._skip_disk_collection = False

    def check(self, instance):
        """Gather VM metrics for each instance"""

        time_start = time.time()

        # Load metric cache
        metric_cache = self._load_metric_cache()

        # Load the nova-obtained instance data cache
        instance_cache = self._load_instance_cache()

        # Build dimensions for both the customer and for operations
        dims_base = self._set_dimensions({'service': 'compute', 'component': 'vm'}, instance)

        # Define aggregate gauges, gauge name to metric name
        agg_gauges = {'vcpus': 'nova.vm.cpu.total_allocated',
                      'ram': 'nova.vm.mem.total_allocated_mb',
                      'disk': 'nova.vm.disk.total_allocated_gb'}
        agg_values = {}
        for gauge in agg_gauges.keys():
            agg_values[gauge] = 0

        insp = inspector.get_hypervisor_inspector()
        updated_cache_this_time = False
        for inst in insp._get_connection().listAllDomains():
            # Verify that this instance exists in the cache.  Add if necessary.
            inst_name = inst.name()
            if inst_name not in instance_cache and not updated_cache_this_time:
                #
                # If we have multiple ghost VMs, we'll needlessly
                # update the instance cache.  Let's limit the cache
                # update to once per agent wakeup.
                #
                updated_cache_this_time = True
                instance_cache = self._update_instance_cache()

            # Build customer dimensions
            try:
                dims_customer = dims_base.copy()
                dims_customer['resource_id'] = instance_cache.get(inst_name)['instance_uuid']
                dims_customer['zone'] = instance_cache.get(inst_name)['zone']
                # Add dimensions that would be helpful for operations
                dims_operations = dims_customer.copy()
                dims_operations['tenant_id'] = instance_cache.get(inst_name)['tenant_id']
                if self.init_config.get('metadata'):
                    for metadata in self.init_config.get('metadata'):
                        metadata_value = (instance_cache.get(inst_name).
                                          get(metadata))
                        if metadata_value:
                            dims_operations[metadata] = metadata_value
                if self.init_config.get('customer_metadata'):
                    for metadata in self.init_config.get('customer_metadata'):
                        metadata_value = (instance_cache.get(inst_name).
                                          get(metadata))
                        if metadata_value:
                            dims_customer[metadata] = metadata_value
                # Remove customer 'hostname' dimension, this will be replaced by the VM name
                del(dims_customer['hostname'])
            except TypeError:
                # Nova can potentially get into a state where it can't see an
                # instance, but libvirt can.  This would cause TypeErrors as
                # incomplete data is cached for this instance.  Log and skip.
                self.log.error("{0} is not known to nova after instance cache update -- skipping this ghost VM.".format(inst_name))
                continue

            # Accumulate aggregate data
            for gauge in agg_gauges:
                if gauge in instance_cache.get(inst_name):
                    agg_values[gauge] += instance_cache.get(inst_name)[gauge]

            # Skip further processing on VMs that are not in an active state
            if self._inspect_state(insp, inst, instance_cache,
                                   dims_customer, dims_operations) != 0:
                continue

            # Skip the remainder of the checks if alive_only is True in the config
            if self.init_config.get('alive_only'):
                continue

            # Skip instances created within the probation period
            vm_probation_remaining = self._test_vm_probation(instance_cache.get(inst_name)['created'])
            if (vm_probation_remaining >= 0):
                self.log.info("Libvirt: {0} in probation for another {1} seconds".format(instance_cache.get(inst_name)['hostname'].encode('utf8'),
                                                                                         vm_probation_remaining))
                continue

            if inst_name not in metric_cache:
                metric_cache[inst_name] = {}

            if self.init_config.get('vm_cpu_check_enable'):
                self._inspect_cpu(insp, inst, inst_name, instance_cache, metric_cache, dims_customer, dims_operations)
            if not self._skip_disk_collection:
                self._last_disk_collect_time = datetime.now()
                if self.init_config.get('vm_disks_check_enable'):
                    self._inspect_disks(insp, inst, inst_name, instance_cache, metric_cache, dims_customer,
                                        dims_operations)
                if self.init_config.get('vm_extended_disks_check_enable'):
                    self._inspect_disk_info(insp, inst, inst_name, instance_cache, metric_cache, dims_customer,
                                            dims_operations)
            if self.init_config.get('vm_network_check_enable'):
                self._inspect_network(insp, inst, inst_name, instance_cache, metric_cache, dims_customer, dims_operations)

            # Memory utilizaion
            # (req. balloon driver; Linux kernel param CONFIG_VIRTIO_BALLOON)
            try:
                mem_stats = inst.memoryStats()
                mem_metrics = {'mem.free_mb': float(mem_stats['unused']) / 1024,
                               'mem.swap_used_mb': float(mem_stats['swap_out']) / 1024,
                               'mem.total_mb': float(mem_stats['available']) / 1024,
                               'mem.used_mb': float(mem_stats['available'] - mem_stats['unused']) / 1024,
                               'mem.free_perc': float(mem_stats['unused']) / float(mem_stats['available']) * 100}
                for name in mem_metrics:
                    self.gauge(name, mem_metrics[name], dimensions=dims_customer,
                               delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                               hostname=instance_cache.get(inst_name)['hostname'])
                    self.gauge("vm.{0}".format(name), mem_metrics[name],
                               dimensions=dims_operations)
                memory_info = insp.inspect_memory_resident(inst)
                self.gauge('vm.mem.resident_mb', float(memory_info.resident), dimensions=dims_operations)
            except KeyError:
                self.log.debug("Balloon driver not active/available on guest {0} ({1})".format(inst_name,
                                                                                               instance_cache.get(inst_name)['hostname']))
            # Test instance's remote responsiveness (ping check) if possible
            if (self.init_config.get('vm_ping_check_enable')) and self.init_config.get('ping_check') and 'network' in instance_cache.get(inst_name):
                for net in instance_cache.get(inst_name)['network']:

                    ping_cmd = self.init_config.get('ping_check').replace('NAMESPACE',
                                                                          net['namespace']).split()
                    ping_cmd.append(net['ip'])
                    dims_customer_ip = dims_customer.copy()
                    dims_operations_ip = dims_operations.copy()
                    dims_customer_ip['ip'] = net['ip']
                    dims_operations_ip['ip'] = net['ip']
                    with open(os.devnull, "w") as fnull:
                        try:
                            self.log.debug("Running ping test: {0}".format(' '.join(ping_cmd)))
                            res = subprocess.call(ping_cmd,
                                                  stdout=fnull,
                                                  stderr=fnull)
                            self.gauge('ping_status', res, dimensions=dims_customer_ip,
                                       delegated_tenant=instance_cache.get(inst_name)['tenant_id'],
                                       hostname=instance_cache.get(inst_name)['hostname'])
                            self.gauge('vm.ping_status', res, dimensions=dims_operations_ip)
                        except OSError as e:
                            self.log.warn("OS error running '{0}' returned {1}".format(ping_cmd, e))

        # Save these metrics for the next collector invocation
        self._update_metric_cache(metric_cache, math.ceil(time.time() - time_start))

        # Publish aggregate metrics
        for gauge in agg_gauges:
            self.gauge(agg_gauges[gauge], agg_values[gauge], dimensions=dims_base)

    def _calculate_rate(self, current_value, cache_value, time_diff):
        """Calculate rate based on current, cache value and time_diff."""
        try:
            rate_value = (current_value - cache_value) / time_diff
        except ZeroDivisionError as e:
            self.log.error("Time difference between current time and "
                           "last_update time is 0 . {0}".format(e))
            #
            # Being extra safe here, in case we divide by zero
            # just skip this reading with check below.
            #
            rate_value = -1
        return rate_value
