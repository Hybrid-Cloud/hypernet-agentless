
# create hypernet.tar.gz
sudo pip install virtualenv
virtualenv /opt/hypernet
source /opt/hypernet/bin/activate
pip install -r requirements.txt
cp clean_hs.sh /opt/hypernet
cp -r ./etc/agent /opt/hypernet/etc/
cp -r ./etc/server/hypernet /opt/hypernet/etc/


# install juno hyperswtich based on 14.04
add-apt-repository -y cloud-archive:juno
apt-get -y update
apt-get -y dist-upgrade
apt-get --no-install-recommends -y install neutron-plugin-ml2 neutron-plugin-openvswitch-agent neutron-l3-agent
apt-get --no-install-recommends -y install openvpn


# endpoint creation sample
keystone service-create --name hypernet --type hypernet --description "OpenStack Hypernet"

keystone endpoint-create \
--service-id $(keystone service-list | awk '/ hypernet / {print $2}') \
--publicurl http://10.10.128.71:8333 \
--adminurl http://10.10.128.71:8333 \
--internalurl http://10.10.128.71:8333 \
--region regionOne

keystone endpoint-create \
--service-id $(keystone service-list | awk '/ hypernet / {print $2}') \
--publicurl http://controller:8333 \
--adminurl http://controller:8333 \
--internalurl http://controller:8333 \
--region regionOne



