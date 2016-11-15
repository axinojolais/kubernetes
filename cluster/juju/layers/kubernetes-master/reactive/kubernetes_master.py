import base64
import os
import random
import socket
import string


from shlex import split
from subprocess import call
from subprocess import check_call
from subprocess import check_output
from subprocess import CalledProcessError

from charms import layer
from charms.reactive import hook
from charms.reactive import remove_state
from charms.reactive import set_state
from charms.reactive import when
from charms.reactive import when_not
from charms.reactive.helpers import data_changed
from charms.kubernetes.flagmanager import FlagManager

from charmhelpers.core import hookenv
from charmhelpers.core import host
from charmhelpers.core.templating import render
from charmhelpers.fetch import apt_install


dashboard_templates = [
    'dashboard-controller.yaml',
    'dashboard-service.yaml',
    'influxdb-grafana-controller.yaml',
    'influxdb-service.yaml',
    'grafana-service.yaml',
    'heapster-controller.yaml',
    'heapster-service.yaml'
]


@hook('upgrade-charm')
def reset_states_for_delivery():
    '''An upgrade charm event was triggered by Juju, react to that here.'''
    services = ['kube-apiserver',
                'kube-controller-manager',
                'kube-scheduler']
    for service in services:
        hookenv.log('Stopping {0} service.'.format(service))
        host.service_stop(service)
    remove_state('kubernetes-master.components.started')
    remove_state('kubernetes-master.components.installed')


@when_not('kubernetes-master.components.installed')
def install():
    '''Unpack and put the Kubernetes master files on the path.'''
    # Get the resource via resource_get
    try:
        archive = hookenv.resource_get('kubernetes')
    except Exception:
        message = 'Error fetching the kubernetes resource.'
        hookenv.log(message)
        hookenv.status_set('blocked', message)
        return

    if not archive:
        hookenv.log('Missing kubernetes resource.')
        hookenv.status_set('blocked', 'Missing kubernetes resource.')
        return

    # Handle null resource publication, we check if filesize < 1mb
    filesize = os.stat(archive).st_size
    if filesize < 1000000:
        hookenv.status_set('blocked', 'Incomplete kubernetes resource.')
        return

    hookenv.status_set('maintenance', 'Unpacking kubernetes resource.')
    files_dir = os.path.join(hookenv.charm_dir(), 'files')

    os.makedirs(files_dir, exist_ok=True)

    command = 'tar -xvzf {0} -C {1}'.format(archive, files_dir)
    hookenv.log(command)
    check_call(split(command))
    dest_dir = '/usr/local/bin/'
    # Create a list of components to install.
    services = ['kube-apiserver',
                'kube-controller-manager',
                'kube-scheduler',
                'kubectl']
    for service in services:
        # Install each one of the service binaries in /usr/local/bin.
        install = 'install -v {0}/{1} {2}'.format(files_dir, service, dest_dir)
        return_code = call(split(install))
        if return_code != 0:
            raise Exception('Unable to install {0}'.format(service))

    set_state('kubernetes-master.components.installed')


@when('kubernetes-master.components.installed')
@when_not('authentication.setup')
def setup_authentication():
    '''Setup basic authentication and token access for the cluster.'''
    api_opts = FlagManager('kube-apiserver')
    controller_opts = FlagManager('kube-controller-manager')

    api_opts.add('--basic-auth-file', '/srv/kubernetes/basic_auth.csv')
    api_opts.add('--token-auth-file', '/srv/kubernetes/known_tokens.csv')
    hookenv.status_set('maintenance', 'Rendering authentication templates.')
    htaccess = '/srv/kubernetes/basic_auth.csv'
    if not os.path.isfile(htaccess):
        setup_basic_auth('admin', 'admin', 'admin')
    known_tokens = '/srv/kubernetes/known_tokens.csv'
    if not os.path.isfile(known_tokens):
        setup_tokens(None, 'admin', 'admin')
        setup_tokens(None, 'kubelet', 'kubelet')
        setup_tokens(None, 'kube_proxy', 'kube_proxy')
    # Generate the default service account token key
    os.makedirs('/etc/kubernetes', exist_ok=True)
    cmd = ['openssl', 'genrsa', '-out', '/etc/kubernetes/serviceaccount.key',
           '2048']
    check_call(cmd)
    api_opts.add('--service-account-key-file',
                 '/etc/kubernetes/serviceaccount.key')
    controller_opts.add('--service-account-private-key-file',
                        '/etc/kubernetes/serviceaccount.key')

    set_state('authentication.setup')


@when('kubernetes-master.components.installed')
def set_app_version():
    ''' Declare the application version to juju '''
    version = check_output(['kube-apiserver', '--version'])
    hookenv.application_version_set(version.split(b' v')[-1].rstrip())


@when('kube-dns.available', 'kube-sdn.configured',
      'kubernetes-master.components.installed')
def ready_messaging():
    ''' Signal at the end of the run that we are running. '''
    hookenv.status_set('active', 'Kubernetes master running.')


@when('etcd.available', 'kubernetes-master.components.installed',
      'kube-sdn.configured', 'certificates.server.cert.available')
@when_not('kubernetes-master.components.started')
def start_master(etcd, tls):
    '''Run the Kubernetes master components.'''
    hookenv.status_set('maintenance',
                       'Rendering the Kubernetes master systemd files.')
    handle_etcd_relation(etcd)
    # Use the etcd relation object to render files with etcd information.
    render_files()
    hookenv.status_set('maintenance',
                       'Starting the Kubernetes master services.')
    services = ['kube-apiserver',
                'kube-controller-manager',
                'kube-scheduler']
    for service in services:
        hookenv.log('Starting {0} service.'.format(service))
        host.service_start(service)
    hookenv.open_port(6443)
    hookenv.status_set('active', 'Kubernetes master services ready.')
    set_state('kubernetes-master.components.started')


@when('cluster-dns.connected', 'sdn-plugin.available')
def send_cluster_dns_detail(cluster_dns, sdn_plugin):
    ''' Send cluster DNS info '''
    # Note that the DNS server doesn't necessarily exist at this point. We know
    # where we're going to put it, though, so let's send the info anyway.
    details = sdn_plugin.get_sdn_config()
    dns_ip = get_dns_ip(details['cidr'])
    cluster_dns.set_dns_info(53, hookenv.config('dns_domain'), dns_ip)


@when('kube-api-endpoint.available')
def push_service_data(kube_api):
    ''' Send configuration to the load balancer, and close access to the
    public interface '''
    kube_api.configure(port=6443)


@when('certificates.available', 'sdn-plugin.available')
def send_data(tls, sdn_plugin):
    '''Send the data that is required to create a server certificate for
    this server.'''
    # Use the public ip of this unit as the Common Name for the certificate.
    common_name = hookenv.unit_public_ip()
    # Get the SDN cidr from the relation object.
    sdn_cidr = sdn_plugin.get_sdn_config().get('cidr')
    # Get the SDN gateway based on the cidr address.
    sdn_ip = get_sdn_ip(sdn_cidr)
    domain = hookenv.config('dns_domain')
    # Create SANs that the tls layer will add to the server cert.
    sans = [
        hookenv.unit_public_ip(),
        hookenv.unit_private_ip(),
        socket.gethostname(),
        sdn_ip,
        'kubernetes',
        'kubernetes.{0}'.format(domain),
        'kubernetes.default',
        'kubernetes.default.svc',
        'kubernetes.default.svc.{0}'.format(domain)
    ]
    # Create a path safe name by removing path characters from the unit name.
    certificate_name = hookenv.local_unit().replace('/', '_')
    # Request a server cert with this information.
    tls.request_server_cert(common_name, sans, certificate_name)


@when('kube-api.connected')
def push_api_data(kube_api):
    ''' Send configuration to remote consumer.'''
    # Since all relations already have the private ip address, only
    # send the port on the relation object to all consumers.
    # The kubernetes api-server uses 6443 for the default secure port.
    kube_api.set_api_port('6443')


@when('kubernetes-master.components.installed', 'sdn-plugin.available')
def gather_sdn_data(sdn_plugin):
    sdn_data = sdn_plugin.get_sdn_config()
    if not sdn_data['cidr'] or not sdn_data['subnet'] or not sdn_data['mtu']:
        hookenv.status_set('waiting', 'Waiting on SDN configuration.')
        return
    api_opts = FlagManager('kube-apiserver')
    api_opts.add('--service-cluster-ip-range', sdn_data['cidr'])
    set_state('kube-sdn.configured')


@when('kubernetes-master.components.started', 'kube-dns.available')
@when_not('kubernetes.dashboard.available')
def install_dashboard_addons():
    ''' Launch dashboard addons if they are enabled in config '''
    if hookenv.config('enable-dashboard-addons'):
        hookenv.log('Launching kubernetes dashboard.')
        context = {}
        context['arch'] = arch()
        context['pillar'] = {'num_nodes': get_node_count()}
        for template in dashboard_templates:
            create_addon(template, context)
        set_state('kubernetes.dashboard.available')


@when('kubernetes-master.components.started', 'kubernetes.dashboard.available')
def remove_dashboard_addons():
    ''' Removes dashboard addons if they are disabled in config '''
    if not hookenv.config('enable-dashboard-addons'):
        hookenv.log('Removing kubernetes dashboard.')
        for template in dashboard_templates:
            delete_addon(template)
        remove_state('kubernetes.dashboard.available')


@when('kubernetes-master.components.installed', 'kube-sdn.configured',
      'sdn-plugin.available')
@when_not('kube-dns.available')
def start_kube_dns(sdn_plugin):
    ''' State guard to starting DNS '''

    # Interrogate the cluster to find out if we have at least one worker
    # that is capable of running the workload.

    cmd = ['kubectl', 'get', 'nodes']
    try:
        out = check_output(cmd)
        if b'NAME' not in out:
            hookenv.log('Unable to determine node count, waiting '
                        'until nodes are ready')
            return
    except CalledProcessError:
        hookenv.log('kube-apiserver not ready, not requesting dns deployment')
        return

    message = 'Rendering the Kubernetes DNS files.'
    hookenv.log(message)
    hookenv.status_set('maintenance', message)

    context = prepare_sdn_context(sdn_plugin)
    context['arch'] = arch()
    create_addon('kubedns-rc.yaml', context)
    create_addon('kubedns-svc.yaml', context)
    set_state('kube-dns.available')


@when('kubernetes-master.components.installed', 'loadbalancer.available',
      'certificates.ca.available', 'certificates.client.cert.available')
def loadbalancer_kubeconfig(loadbalancer, ca, client):
    # Get the potential list of loadbalancers from the relation object.
    hosts = loadbalancer.get_addresses_ports()
    # Get the public address of loadbalancers so users can access the cluster.
    address = hosts[0].get('public-address')
    # Get the port of the loadbalancer so users can access the cluster.
    port = hosts[0].get('port')
    server = 'https://{0}:{1}'.format(address, port)
    build_kubeconfig(server)


@when('kubernetes-master.components.installed',
      'certificates.ca.available', 'certificates.client.cert.available')
@when_not('loadbalancer.available')
def create_self_config(ca, client):
    '''Create a kubernetes configuration for the master unit.'''
    server = 'https://{0}:{1}'.format(hookenv.unit_get('public-address'), 6443)
    build_kubeconfig(server)


@when('ceph-storage.available')
def ceph_state_control(ceph_admin):
    ''' Determine if we should remove the state that controls the re-render
    and execution of the ceph-relation-changed event because there
    are changes in the relationship data, and we should re-render any
    configs, keys, and/or service pre-reqs '''

    ceph_relation_data = {
        'mon_hosts': ceph_admin.mon_hosts(),
        'fsid': ceph_admin.fsid(),
        'auth_supported': ceph_admin.auth(),
        'hostname': socket.gethostname(),
        'key': ceph_admin.key()
    }

    # Re-execute the rendering if the data has changed.
    if data_changed('ceph-config', ceph_relation_data):
        remove_state('ceph-storage.configured')


@when('ceph-storage.available')
@when_not('ceph-storage.configured')
def ceph_storage(ceph_admin):
    '''Ceph on kubernetes will require a few things - namely a ceph
    configuration, and the ceph secret key file used for authentication.
    This method will install the client package, and render the requisit files
    in order to consume the ceph-storage relation.'''
    ceph_context = {
        'mon_hosts': ceph_admin.mon_hosts(),
        'fsid': ceph_admin.fsid(),
        'auth_supported': ceph_admin.auth(),
        'use_syslog': "true",
        'ceph_public_network': '',
        'ceph_cluster_network': '',
        'loglevel': 1,
        'hostname': socket.gethostname(),
    }
    # Install the ceph common utilities.
    apt_install(['ceph-common'], fatal=True)

    etc_ceph_directory = '/etc/ceph'
    if not os.path.isdir(etc_ceph_directory):
        os.makedirs(etc_ceph_directory)
    charm_ceph_conf = os.path.join(etc_ceph_directory, 'ceph.conf')
    # Render the ceph configuration from the ceph conf template
    render('ceph.conf', charm_ceph_conf, ceph_context)

    # The key can rotate independently of other ceph config, so validate it
    admin_key = os.path.join(etc_ceph_directory,
                             'ceph.client.admin.keyring')
    try:
        with open(admin_key, 'w') as key_file:
            key_file.write("[client.admin]\n\tkey = {}\n".format(
                ceph_admin.key()))
    except IOError as err:
        hookenv.log("IOError writing admin.keyring: {}".format(err))

    # Enlist the ceph-admin key as a kubernetes secret
    if ceph_admin.key():
        encoded_key = base64.b64encode(ceph_admin.key().encode('utf-8'))
    else:
        # We didn't have a key, and cannot proceed. Do not set state and
        # allow this method to re-execute
        return

    context = {'secret': encoded_key.decode('ascii')}
    render('ceph-secret.yaml', '/tmp/ceph-secret.yaml', context)
    try:
        # At first glance this is deceptive. The apply stanza will create if
        # it doesn't exist, otherwise it will update the entry, ensuring our
        # ceph-secret is always reflective of what we have in /etc/ceph
        # assuming we have invoked this anytime that file would change.
        cmd = ['kubectl', 'apply', '-f', '/tmp/ceph-secret.yaml']
        check_call(cmd)
        os.remove('/tmp/ceph-secret.yaml')
    except:
        # the enlistment in kubernetes failed, return and prepare for re-exec
        return

    # when complete, set a state relating to configuration of the storage
    # backend that will allow other modules to hook into this and verify we
    # have performed the necessary pre-req steps to interface with a ceph
    # deployment.
    set_state('ceph-storage.configured')


def create_addon(template, context):
    '''Create an addon from a template'''
    source = 'addons/' + template
    target = '/etc/kubernetes/addons/' + template
    render(source, target, context)
    cmd = ['kubectl', 'apply', '-f', target]
    check_call(cmd)


def delete_addon(template):
    '''Delete an addon from a template'''
    target = '/etc/kubernetes/addons/' + template
    cmd = ['kubectl', 'delete', '-f', target]
    call(cmd)


def get_node_count():
    '''Return the number of Kubernetes nodes in the cluster'''
    cmd = ['kubectl', 'get', 'nodes', '-o', 'name']
    output = check_output(cmd)
    node_count = len(output.splitlines())
    return node_count


def arch():
    '''Return the package architecture as a string. Raise an exception if the
    architecture is not supported by kubernetes.'''
    # Get the package architecture for this system.
    architecture = check_output(['dpkg', '--print-architecture']).rstrip()
    # Convert the binary result into a string.
    architecture = architecture.decode('utf-8')
    return architecture


def build_kubeconfig(server):
    '''Gather the relevant data for Kubernetes configuration objects and create
    a config object with that information.'''
    # Get the options from the tls-client layer.
    layer_options = layer.options('tls-client')
    # Get all the paths to the tls information required for kubeconfig.
    ca = layer_options.get('ca_certificate_path')
    ca_exists = ca and os.path.isfile(ca)
    key = layer_options.get('client_key_path')
    key_exists = key and os.path.isfile(key)
    cert = layer_options.get('client_certificate_path')
    cert_exists = cert and os.path.isfile(cert)
    # Do we have everything we need?
    if ca_exists and key_exists and cert_exists:
        # Cache last server string to know if we need to regenerate the config.
        if not data_changed('kubeconfig.server', server):
            return
        # The final destination of the kubeconfig and kubectl.
        destination_directory = '/home/ubuntu'
        # Create an absolute path for the kubeconfig file.
        kubeconfig_path = os.path.join(destination_directory, 'config')
        # Create the kubeconfig on this system so users can access the cluster.
        create_kubeconfig(kubeconfig_path, server, ca, key, cert)
        # Copy the kubectl binary to the destination directory.
        cmd = ['install', '-v', '-o', 'ubuntu', '-g', 'ubuntu',
               '/usr/local/bin/kubectl', destination_directory]
        check_call(cmd)
        # Make the config file readable by the ubuntu users so juju scp works.
        cmd = ['chown', 'ubuntu:ubuntu', kubeconfig_path]
        check_call(cmd)


def create_kubeconfig(kubeconfig, server, ca, key, certificate, user='ubuntu',
                      context='juju-context', cluster='juju-cluster'):
    '''Create a configuration for Kubernetes based on path using the supplied
    arguments for values of the Kubernetes server, CA, key, certificate, user
    context and cluster.'''
    # Create the config file with the address of the master server.
    cmd = 'kubectl config --kubeconfig={0} set-cluster {1} ' \
          '--server={2} --certificate-authority={3} --embed-certs=true'
    check_call(split(cmd.format(kubeconfig, cluster, server, ca)))
    # Create the credentials using the client flags.
    cmd = 'kubectl config --kubeconfig={0} set-credentials {1} ' \
          '--client-key={2} --client-certificate={3} --embed-certs=true'
    check_call(split(cmd.format(kubeconfig, user, key, certificate)))
    # Create a default context with the cluster.
    cmd = 'kubectl config --kubeconfig={0} set-context {1} ' \
          '--cluster={2} --user={3}'
    check_call(split(cmd.format(kubeconfig, context, cluster, user)))
    # Make the config use this new context.
    cmd = 'kubectl config --kubeconfig={0} use-context {1}'
    check_call(split(cmd.format(kubeconfig, context)))


def get_dns_ip(cidr):
    '''Get an IP address for the DNS server on the provided cidr.'''
    # Remove the range from the cidr.
    ip = cidr.split('/')[0]
    # Take the last octet off the IP address and replace it with 10.
    return '.'.join(ip.split('.')[0:-1]) + '.10'


def get_sdn_ip(cidr):
    '''Get the IP address for the SDN gateway based on the provided cidr.'''
    # Remove the range from the cidr.
    ip = cidr.split('/')[0]
    # Remove the last octet and replace it with 1.
    return '.'.join(ip.split('.')[0:-1]) + '.1'


def handle_etcd_relation(reldata):
    ''' Save the client credentials and set appropriate daemon flags when
    etcd declares itself as available'''
    connection_string = reldata.get_connection_string()
    # Define where the etcd tls files will be kept.
    etcd_dir = '/etc/ssl/etcd'
    # Create paths to the etcd client ca, key, and cert file locations.
    ca = os.path.join(etcd_dir, 'client-ca.pem')
    key = os.path.join(etcd_dir, 'client-key.pem')
    cert = os.path.join(etcd_dir, 'client-cert.pem')

    # Save the client credentials (in relation data) to the paths provided.
    reldata.save_client_credentials(key, cert, ca)

    api_opts = FlagManager('kube-apiserver')

    # Never use stale data, always prefer whats coming in during context
    # building. if its stale, its because whats in unitdata is stale
    data = api_opts.data
    if data.get('--etcd-servers-strict') or data.get('--etcd-servers'):
        api_opts.destroy('--etcd-cafile')
        api_opts.destroy('--etcd-keyfile')
        api_opts.destroy('--etcd-certfile')
        api_opts.destroy('--etcd-servers', strict=True)
        api_opts.destroy('--etcd-servers')

    # Set the apiserver flags in the options manager
    api_opts.add('--etcd-cafile', ca)
    api_opts.add('--etcd-keyfile', key)
    api_opts.add('--etcd-certfile', cert)
    api_opts.add('--etcd-servers', connection_string, strict=True)


def prepare_sdn_context(sdn_plugin=None):
    '''Get the Software Defined Network (SDN) information and return it as a
    dictionary. '''
    sdn_data = {}
    # The dictionary named 'pillar' is a construct of the k8s template files.
    pillar = {}
    # SDN Providers pass data via the sdn-plugin interface
    # Ideally the DNS address should come from the sdn cidr.
    plugin_data = sdn_plugin.get_sdn_config()
    if plugin_data.get('cidr'):
        # Generate the DNS ip address on the SDN cidr (this is desired).
        pillar['dns_server'] = get_dns_ip(plugin_data['cidr'])
    # The pillar['dns_server'] value is used the kubedns-svc.yaml file.
    pillar['dns_replicas'] = 1
    # The pillar['dns_domain'] value is used in the kubedns-rc.yaml
    pillar['dns_domain'] = hookenv.config('dns_domain')
    # Use a 'pillar' dictionary so we can reuse the upstream kubedns templates.
    sdn_data['pillar'] = pillar
    return sdn_data


def render_files():
    '''Use jinja templating to render the docker-compose.yml and master.json
    file to contain the dynamic data for the configuration files.'''
    context = {}
    config = hookenv.config()
    # Add the charm configuration data to the context.
    context.update(config)

    # Update the context with extra values: arch, and networking information
    context.update({'arch': arch(),
                    'master_address': hookenv.unit_get('private-address'),
                    'public_address': hookenv.unit_get('public-address'),
                    'private_address': hookenv.unit_get('private-address')})

    api_opts = FlagManager('kube-apiserver')
    controller_opts = FlagManager('kube-controller-manager')
    scheduler_opts = FlagManager('kube-scheduler')

    # Get the tls paths from the layer data.
    layer_options = layer.options('tls-client')
    ca_cert_path = layer_options.get('ca_certificate_path')
    server_cert_path = layer_options.get('server_certificate_path')
    server_key_path = layer_options.get('server_key_path')

    # Handle static options for now
    api_opts.add('--min-request-timeout', '300')
    api_opts.add('--v', '4')
    api_opts.add('--client-ca-file', ca_cert_path)
    api_opts.add('--tls-cert-file', server_cert_path)
    api_opts.add('--tls-private-key-file', server_key_path)

    scheduler_opts.add('--v', '2')

    # Default to 3 minute resync. TODO: Make this configureable?
    controller_opts.add('--min-resync-period', '3m')
    controller_opts.add('--v', '2')
    controller_opts.add('--root-ca-file', ca_cert_path)

    context.update({'kube_apiserver_flags': api_opts.to_s(),
                    'kube_scheduler_flags': scheduler_opts.to_s(),
                    'kube_controller_manager_flags': controller_opts.to_s()})

    # Render the configuration files that contains parameters for
    # the apiserver, scheduler, and controller-manager
    render_service('kube-apiserver', context)
    render_service('kube-controller-manager', context)
    render_service('kube-scheduler', context)

    # explicitly render the generic defaults file
    render('kube-defaults.defaults', '/etc/default/kube-defaults', context)

    # when files change on disk, we need to inform systemd of the changes
    call(['systemctl', 'daemon-reload'])
    call(['systemctl', 'enable', 'kube-apiserver'])
    call(['systemctl', 'enable', 'kube-controller-manager'])
    call(['systemctl', 'enable', 'kube-scheduler'])


def render_service(service_name, context):
    '''Render the systemd service by name.'''
    unit_directory = '/lib/systemd/system'
    source = '{0}.service'.format(service_name)
    target = os.path.join(unit_directory, '{0}.service'.format(service_name))
    render(source, target, context)
    conf_directory = '/etc/default'
    source = '{0}.defaults'.format(service_name)
    target = os.path.join(conf_directory, service_name)
    render(source, target, context)


def setup_basic_auth(username='admin', password='admin', user='admin'):
    '''Create the htacces file and the tokens.'''
    srv_kubernetes = '/srv/kubernetes'
    if not os.path.isdir(srv_kubernetes):
        os.makedirs(srv_kubernetes)
    htaccess = os.path.join(srv_kubernetes, 'basic_auth.csv')
    with open(htaccess, 'w') as stream:
        stream.write('{0},{1},{2}'.format(username, password, user))


def setup_tokens(token, username, user):
    '''Create a token file for kubernetes authentication.'''
    srv_kubernetes = '/srv/kubernetes'
    if not os.path.isdir(srv_kubernetes):
        os.makedirs(srv_kubernetes)
    known_tokens = os.path.join(srv_kubernetes, 'known_tokens.csv')
    if not token:
        alpha = string.ascii_letters + string.digits
        token = ''.join(random.SystemRandom().choice(alpha) for _ in range(32))
    with open(known_tokens, 'w') as stream:
        stream.write('{0},{1},{2}'.format(token, username, user))
