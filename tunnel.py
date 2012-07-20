import os, random, traceback, time
import plib, utils, db

log = None
smooth = 0.3

class Connection:
    def __init__(self, address, write_pipe, hello, iface, prefix,
            ovpn_args):
        self.process = plib.client(address, write_pipe, hello, '--dev', iface,
                *ovpn_args, stdout=os.open(os.path.join(log,
                'vifibnet.client.%s.log' % (prefix,)),
                os.O_WRONLY|os.O_CREAT|os.O_TRUNC) )

        self.iface = iface
        self._prefix = prefix
        self._creation_date = time.time()
        self._bandwidth = None
        self._last_trafic = None

    # TODO : update the stats
    def refresh(self):
        # Check that the connection is alive
        if self.process.poll() != None:
            utils.log('Connection with %s has failed with return code %s' 
                     % (self._prefix, self.process.returncode), 3)
            return False

        self._updateBandwidth()
        return True

    def _updateBandwidth(self):
        try:
            f_rx = open('/sys/class/net/%s/statistics/rx_bytes' %
                    self.iface, 'r')
            f_tx = open('/sys/class/net/%s/statistics/tx_bytes' %
                    self.iface, 'r')

            trafic = int(f_rx.read()) + int(f_tx.read())
            t = time.time()

            if bool(self._last_trafic):
                bw = (trafic - self._last_trafic)/(t - 
                        self._last_trafic_update)
                if bool(self._bandwidth):
                    self._bandwidth = (1-smooth)*self._bandwidth + smooth*bw
                else:
                    self._bandwidth = bw

                utils.log('New bandwidth calculated on iface %s : %s' % 
                        (self.iface, self._bandwidth), 4)

            self._last_trafic_update = t
            self._last_trafic = trafic
        except IOError: # This just means that the interface is downs
            utils.log('Unable to calculate bandwidth on iface %s' % 
                self.iface, 4)

class TunnelManager:

    def __init__(self, write_pipe, peer_db, openvpn_args, hello_interval,
                refresh, connection_count, refresh_rate):
        self._write_pipe = write_pipe
        self._peer_db = peer_db
        self._connection_dict = {}
        self._ovpn_args = openvpn_args
        self._hello = hello_interval
        self._refresh_time = refresh
        self.free_interface_set = set(('client1', 'client2', 'client3',
                                       'client4', 'client5', 'client6',
                                       'client7', 'client8', 'client9',
                                       'client10', 'client11', 'client12'))
        self.next_refresh = time.time()

        self._client_count = connection_count/2
        self._refresh_count = refresh_rate*self._client_count

    def refresh(self):
        utils.log('Refreshing the tunnels', 2)
        self._cleanDeads()
        self._removeSomeTunnels()
        self._makeNewTunnels()
        self.next_refresh = time.time() + self._refresh_time

    def _cleanDeads(self):
        for prefix in self._connection_dict.keys():
            if not self._connection_dict[prefix].refresh():
                self._kill(prefix)

    def _removeSomeTunnels(self):
        for i in range(0, max(0, len(self._connection_dict) -
                    self._client_count + self._refresh_count)):
            prefix = random.choice(self._connection_dict.keys())
            self._kill(prefix)

    def _kill(self, prefix):
        utils.log('Killing the connection with ' + prefix, 2)
        connection = self._connection_dict.pop(prefix)
        try:
            connection.process.kill()
        except OSError:
            # If the process is already exited
            pass
        self.free_interface_set.add(connection.iface)
        self._peer_db.unusePeer(prefix)

    def _makeNewTunnels(self):
        utils.log('Trying to make %i new tunnels' %
                (self._client_count - len(self._connection_dict)), 5)
        try:
            for prefix, address in self._peer_db.getUnusedPeers(
                    self._client_count - len(self._connection_dict)):
                utils.log('Establishing a connection with %s' % prefix, 2)
                iface = self.free_interface_set.pop()
                self._connection_dict[prefix] = Connection(address,
                        self._write_pipe, self._hello, iface,
                        prefix, self._ovpn_args)
                self._peer_db.usePeer(prefix)
        except KeyError:
            utils.log("""Can't establish connection with %s
                    : no available interface""" % prefix, 2)
        except Exception:
            traceback.print_exc()
