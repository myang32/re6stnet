#!/usr/bin/env python
import argparse, errno, os, sqlite3, subprocess, sys, time
import traceback
import upnpigd
import openvpn
import random

VIFIB_NET = "2001:db8:42::/48"
connection_dict = {} # to remember current connections
free_interface_set = set(('client1', 'client2', 'client3', 'client4', 'client5', 
                          'client6', 'client7', 'client8', 'client9', 'client10'))

def log_message(message, verbose_level):
    if config.verbose >= verbose_level:
        print time.strftime("%d-%m-%Y %H:%M:%S : " + message)

# TODO : How do we get our vifib ip ?

def babel(network_ip, network_mask, verbose_level):
    args = ['babeld',
            '-C', 'redistribute local ip %s/%s' % (network_ip, network_mask),
            '-C', 'redistribute local deny',
            # Route VIFIB ip adresses
            '-C', 'in ip %s' % VIFIB_NET,
            # Route only addresse in the 'local' network,
            # or other entire networks
            '-C', 'in ip %s/%s' % (network_ip,network_mask),
            #'-C', 'in ip ::/0 le %s' % network_mask,
            # Don't route other addresses
            '-C', 'in ip deny',
            '-d', str(verbose_level),
            '-s',
            ]
    if config.babel_state:
        args += '-S', config.babel_state
    log_message("Starting babel daemon",2)
    return subprocess.Popen(args + list(free_interface_set))

def getConfig():
    global config
    parser = argparse.ArgumentParser(
            description='Resilient virtual private network application')
    _ = parser.add_argument
    _('--server-log', default='/var/log/vifibnet.server.log',
            help='Path to openvpn server log file')
    _('--client-log', default='/var/log/',
            help='Path to openvpn client log directory')
    _('--client-count', default=2, type=int,
            help='the number servers the peers try to connect to')
    # TODO : use maxpeer
    _('--max-peer', default=10, type=int,
            help='the number of peers that can connect to the server')
    _('--refresh-time', default=20, type=int,
            help='the time (seconds) to wait before changing the connections')
    _('--refresh-count', default=1, type=int,
            help='The number of connections to drop when refreshing the connections')
    _('--db', default='/var/lib/vifibnet/peers.db',
            help='Path to peers database')
    _('--dh', required=True,
            help='Path to dh file')
    _('--babel-state', default='/var/lib/vifibnet/babel_state',
            help='Path to babeld state-file')
    _('--verbose', '-v', default=0, type=int,
            help='Defines the verbose level')
    # Temporary args
    _('--ip', required=True,
            help='IPv6 of the server')
    # Openvpn options
    _('openvpn_args', nargs=argparse.REMAINDER,
            help="Common OpenVPN options (e.g. certificates)")
    openvpn.config = config = parser.parse_args()
    if config.openvpn_args[0] == "--":
        del config.openvpn_args[0]

def startNewConnection(n):
    try:
        for id, ip, port, proto in peer_db.execute(
            "SELECT id, ip, port, proto FROM peers WHERE used = 0 ORDER BY RANDOM() LIMIT ?", (n,)):
            log_message('Establishing a connection with id %s (%s:%s)' % (id,ip,port), 2)
            iface = free_interface_set.pop()
            connection_dict[id] = ( openvpn.client( ip, '--dev', iface, '--proto', proto, '--rport', str(port),
                stdout=os.open(config.client_log + 'vifibnet.client.' + str(id) + '.log', os.O_RDONLY|os.O_CREAT) ) , iface)
            log_message('Updating peers database', 3)
            peer_db.execute("UPDATE peers SET used = 1 WHERE id = ?", (id,))
    except KeyError:
        log_message("Can't establish connection with %s : no available interface" % ip, 2)
        pass
    except Exception:
        traceback.print_exc()

def killConnection(id):
    try:
        log_message('Killing the connection with id ' + str(id), 2)
        p, iface = connection_dict.pop(id)
        p.kill()
        free_interface_set.add(iface)
        log_message('Updating peers database', 3)
        peer_db.execute("UPDATE peers SET used = 0 WHERE id = ?", (id,))
    except KeyError:
        log_message("Can't kill connection to " + peer + ": no existing connection", 1)
        pass
    except Exception:
        log_message("Can't kill connection to " + peer + ": uncaught error", 1)
        pass


def refreshConnections():
    # Kill some random connections
    try:
        for i in range(0, int(config.refresh_count)):
            id = random.choice(connection_dict.keys())
            killConnection(id)
    except Exception:
        pass
    # Establish new connections
    startNewConnection(config.client_count - len(connection_dict))

def main():
    # Get arguments
    getConfig()
    (externalIp, externalPort) = upnpigd.GetExternalInfo(1194)

    # Setup database
    global peer_db # stop using global variables for everything ?
    log_message('Connectiong to peers database', 4)
    peer_db = sqlite3.connect(config.db, isolation_level=None)
    log_message('Initializing peers database', 4)
    peer_db.execute("""CREATE TABLE IF NOT EXISTS peers
             ( id INTEGER PRIMARY KEY AUTOINCREMENT,
             ip TEXT NOT NULL,
             port INTEGER NOT NULL,
             proto TEXT NOT NULL,
             used INTEGER NOT NULL)""")
    peer_db.execute("CREATE INDEX IF NOT EXISTS _peers_used ON peers(used)")
    peer_db.execute("UPDATE peers SET used = 0")

    # Establish connections
    log_message('Starting openvpn server', 3)
    serverProcess = openvpn.server(config.ip,
            '--dev', 'vifibnet', stdout=os.open(config.server_log, os.O_RDONLY|os.O_CREAT))
    startNewConnection(config.client_count)

    # main loop
    try:
        while True:
            # TODO : use select to get openvpn events from pipes
            time.sleep(float(config.refresh_time))
            refreshConnections()
    except KeyboardInterrupt:
        return 0

if __name__ == "__main__":
    main()

# TODO : remove incomming connections from avalaible peers

