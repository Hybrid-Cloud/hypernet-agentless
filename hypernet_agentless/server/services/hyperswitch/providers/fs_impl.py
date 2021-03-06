
import time

from hypernet_agentless.common import hs_constants
from hypernet_agentless.server import config
from hypernet_agentless.server.extensions import hyperswitch
from hypernet_agentless.server.services.hyperswitch import provider_api

from oslo_log import log as logging
from hypernet_agentless.server.services import os_client


LOG = logging.getLogger(__name__)

HS_START_NAME = '%s-' % hs_constants.HYPERSWITCH

class FSProvider(provider_api.ProviderDriver):
    
    def __init__(self):
        self._cfg = config
        self._net_ids = {}
        self._vm_nets = []

    @property
    def _neutron_client(self):
        return os_client.get_neutron_client('hyperswitch_fs')

    @property
    def _nova_client(self):
        return os_client.get_nova_client('hyperswitch_fs')

    def _get_net_id(self, id_or_name):
        if not id_or_name in self._net_ids:
            try:
                self._net_ids[id_or_name] = (
                    self._neutron_client.show_network(
                        id_or_name)['network']['id']
                )
            except:
                self._net_ids[id_or_name] = (
                    self._neutron_client.list_networks(
                        name=id_or_name)['networks'][0]['id']
                )
        return self._net_ids[id_or_name]

    def _net_equal(self, net1, net2):
        return self._get_net_id(net1) == self._get_net_id(net2) 

    def _find_image(self, key, value):
        images = self._nova_client.images.list()
        for image in images:
            if key in image.metadata and image.metadata[key] == value:
                return image
        return None

    def _find_flavor(self, name):
        for flavor in self._nova_client.flavors.list():
            if flavor.name == name:
                return flavor
        return None

    def _fs_instance_to_dict(self, fs_instance):
        LOG.debug('_fs_instance_to_dict %s' % fs_instance)
        LOG.debug('_fs_instance_to_dict networks %s' % fs_instance.networks)
        vm_nets = self.get_vms_subnet()
        vms_ips = []
        mgnt_ip = None
        data_ip = None
        LOG.debug('_fs_instance_to_dict vm_nets %s' % vm_nets)
        for net_int in fs_instance.networks:
            if self._net_equal(net_int, self._cfg.mgnt_network()):
                mgnt_ip = fs_instance.networks[net_int][0]
            if self._net_equal(net_int, self._cfg.data_network()):
                data_ip = fs_instance.networks[net_int][0]
            i = 0
            for net in vm_nets:
                if self._net_equal(net_int, net):
                    vms_ips.append({
                        'vms_ip': fs_instance.networks[net_int][0],
                        'index': i
                    })
                i = i + 1
        res = provider_api.ProviderHyperswitch(
           instance_id=fs_instance.id,
           name=fs_instance.name,
           instance_type=self._get_flavor_name(fs_instance.flavor['id']),
           mgnt_ip=mgnt_ip,
           data_ip=data_ip,
           vms_ips=vms_ips,
        ).dict
        LOG.debug('_fs_instance_to_dict result %s' % res)
        return res

    def _get_flavor_name(self, flavor_id):
        LOG.debug('flavor_id %s' % flavor_id)
        try:
            return self._nova_client.flavors.get(flavor_id).name
        except:
            return flavor_id

    def get_sgs(self):
        hs_sg, vm_sg = None, None
        security_groups = self._neutron_client.list_security_groups(
            name=[self._cfg.hs_sg_name(), self._cfg.vm_sg_name()]
        )['security_groups']
        if len(security_groups) > 0:
            for sg in security_groups:
                if sg['name'] == self._cfg.hs_sg_name():
                    hs_sg = sg['id']
                if sg['name'] == self._cfg.vm_sg_name():
                    vm_sg = sg['id']
        else:
            hs_sg = self._neutron_client.create_security_group(
                {'security_group': {
                    'name': self._cfg.hs_sg_name(),
                    'description': ('%s security group' %
                                    self._cfg.hs_sg_name()),
                    'tenant_id': self._cfg.fs_tenant_id()
                }}
            )['security_group']['id']
            vm_sg = self._neutron_client.create_security_group({
                'security_group': {
                    'name': self._cfg.vm_sg_name(),
                    'description': ('%s security group' %
                                    self._cfg.hs_sg_name()),
                    'tenant_id': self._cfg.fs_tenant_id()
                }}
            )['security_group']['id']
            self._neutron_client.create_security_group_rule({
                'security_group_rule': {
                    'direction': 'ingress',
                    'ethertype': 'IPv4',
                    'remote_group_id': hs_sg,
                    'security_group_id': vm_sg,
                    'tenant_id': self._cfg.fs_tenant_id()
                }}
            )
            self._neutron_client.create_security_group_rule({
                'security_group_rule': {
                    'direction': 'ingress',
                    'ethertype': 'IPv4',
                    'remote_group_id': vm_sg,
                    'security_group_id': hs_sg,
                    'tenant_id': self._cfg.fs_tenant_id()
                }}
            )
        return hs_sg, vm_sg

    def get_vms_subnet(self):
        if len(self._vm_nets) != len(self._cfg.vms_cidr()):
            for cidr in self._cfg.vms_cidr():
                snets = self._neutron_client.list_subnets(cidr=cidr)['subnets']
                if len(snets) > 0:
                    self._vm_nets = self._vm_nets + snets
                else:
                    name = 'vms_%s' % cidr
                    nets = self._neutron_client.list_networks(
                        name=name)['networks']
                    if len(nets) > 0:
                        net = nets[0]
                    else:
                        net = self._neutron_client.create_network({
                            'network': {
                                'name': name
                            }
                        })['network']
                    snet = self._neutron_client.create_subnet({
                        'subnet': {
                            'name': name,
                            'cidr': cidr,
                            'enable_dhcp': 'true',
                            'network_id': net['id'],
                            'ip_version': 4,
                        }
                    })['subnet']
                    self._vm_nets = self._vm_nets + [snet]
        subnets_id = []
        for net in self._vm_nets:
            subnets_id.append(net['network_id'])
        return subnets_id

    def create_hyperswitch(self,
                           user_data,
                           flavor,
                           net_list,
                           hyperswitch_id):
        LOG.debug('create hyper switch %s, %s, %s, %s' % (
            user_data, flavor, net_list, hyperswitch_id))
        hs_instance = self.get_hyperswitch(hyperswitch_id)
        if hs_instance:
            return hs_instance

        hs_img = self._find_image('hybrid_cloud_image',
                                  hs_constants.HYPERSWITCH)
        hs_flavor = self._find_flavor(self._cfg.hs_flavor_map()[flavor])
        user_metadata = None
        if user_data:
            user_metadata = ''
            for k, v in user_data.iteritems():
                user_metadata = '%s\n%s=%s' % (user_metadata, k, v)

        nics = []
        for net in net_list:
            port = self._neutron_client.create_port({
                'port': {
                    'security_groups': net['security_group'],
                    'tenant_id': self._cfg.fs_tenant_id(),
                    'network_id': net['name']
            }})['port']
            nics.append({'port-id': port['id']})
        
        meta = {
            'hybrid_cloud_type': hs_constants.HYPERSWITCH
        }
        hs_instance = self._nova_client.servers.create(
             hyperswitch_id,
             hs_img,
             hs_flavor,
             meta=meta,
             nics=nics,
             userdata=user_metadata,
             availability_zone=self._cfg.fs_availability_zone())
        nb_retry = 0
        while len(hs_instance.networks) == 0 and nb_retry < 100:
            time.sleep(2)
            for inst in self._nova_client.servers.list(
                search_opts={'name': hyperswitch_id}):
                hs_instance = inst
            nb_retry = nb_retry + 1
        if nb_retry == 100:
            self._nova_client.servers.delete(hs_instance.id)
            raise hyperswitch.HyperswitchVMCreationFailed(
                    hyperswitch_id=hyperswitch_id,
                    hyperswitch_status=hs_instance.status)
        return self._fs_instance_to_dict(hs_instance)

    def get_hyperswitch(self, hyperswitch_id):
        LOG.debug('get hyperswitch %s.' % hyperswitch_id)
        i = 0
        res = None
        hss = self._nova_client.servers.list(
            search_opts={'name': hyperswitch_id})
        for hs in hss:
            if i != 0:
                raise hyperswitch.HyperswitchProviderMultipleFound(
                    hyperswitch_id=hyperswitch_id)
            res = self._fs_instance_to_dict(hs)
            i = i + 1
        LOG.debug('get hyperswitch %s result %s.' % (hyperswitch_id, res))
        return res
            
    def start_hyperswitch(self, hyperswitch_id):
        LOG.debug('start hyperswitchs %s.' % hyperswitch_id)
        hss = self._nova_client.servers.list(
            search_opts={'name': hyperswitch_id})
        for hs in hss:
            if not hs.status in ['ACTIVE', 'BUILD']:
                self._nova_client.servers.start(hs.id)
       
    def stop_hyperswitch(self, hyperswitch_id):
        LOG.debug('stop hyperswitch %s.' % hyperswitch_id)
        hss = self._nova_client.servers.list(
            search_opts={'name': hyperswitch_id})
        for hs in hss:
            if hs.status in ['ACTIVE']:
                self._nova_client.servers.stop(hs.id)

    def delete_hyperswitch(self, hyperswitch_id):
        LOG.debug('hyperswitch to delete: %s.' % (hyperswitch_id))
        hss = self._nova_client.servers.list(
            search_opts={'name': hyperswitch_id})
        for hs in hss:
            n_ports = self._neutron_client.list_ports(
                device_id=[hs.id])['ports']
            self._nova_client.servers.delete(hs.id)
            for n_port in n_ports:
                self._neutron_client.delete_port(n_port['id'])

    def _to_net_int(self, port):
        return provider_api.ProviderPort(
           port_id=port['id'],
           provider_ip=port['fixed_ips'][0]['ip_address'],
           name=port['name'],
        ).dict
    
    def create_network_interface(self,
                                 port_id,
                                 subnet,
                                 security_group):
        LOG.debug('create net interface (%s, %s, %s).' % (
            port_id, subnet, security_group))
        ports = self._neutron_client.list_ports(name=[port_id])['ports']
        if len(ports) == 0 :
            port = self._neutron_client.create_port({'port': {
                'name': port_id,
                'tenant_id': self._cfg.fs_tenant_id(),
                'security_groups': [security_group],
                'network_id': subnet
            }})['port']
        else:
            if len(ports) != 1:
                raise hyperswitch.ProviderPortProviderPortMultipleFound(
                    providerport_id=port_id)
            port = ports[0]
        LOG.debug('port: %s.' % (port))
        return self._to_net_int(port)

    def delete_network_interface(self, port_id):
        LOG.debug('delete net interface (%s).' % (port_id))
        ports = self._neutron_client.list_ports(name=[port_id])['ports']
        for port in ports:
            self._neutron_client.delete_port(port['id'])

    def _add_net_int(self, ports_res, res):
        for port in ports_res['ports']:
            res.append(self._to_net_int(port))

    def get_network_interface(self, port_id):
        LOG.debug('get network interface %s.' % port_id)
        i = 0
        res = None
        ports = self._neutron_client.list_ports(name=[port_id])['ports']
        for port in ports:
            if i != 0:
                raise hyperswitch.ProviderPortProviderPortMultipleFound(
                    providerport_id=port_id)
            res = self._to_net_int(port)
            i = i + 1
        LOG.debug('get network interface %s result = %s.' % (port_id, res))
        return res
