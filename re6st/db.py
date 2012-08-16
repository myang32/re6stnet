import logging, sqlite3, socket, subprocess, xmlrpclib, time
import utils

# used = 0 : fresh node
# used = 1 : previously used peer
# used = 2 : curently in use


class PeerManager:

    # internal ip = temp arg/attribute
    def __init__(self, db_path, registry, key_path, refresh_time, address,
                       internal_ip, prefix, manual, pp, db_size):
        self._refresh_time = refresh_time
        self.address = address
        self._internal_ip = internal_ip
        self._prefix = prefix
        self.db_size = db_size
        self._registry = registry
        self._key_path = key_path
        self._pp = pp
        self._manual = manual
        self.tunnel_manager = None

        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        self.sock.bind(('::', 326))
        self.socket_file = self.sock.makefile()

        logging.info('Connecting to peers database...')
        self._db = sqlite3.connect(db_path, isolation_level=None)
        logging.debug('Database opened')

        logging.info('Preparing peers database...')
        self._db.execute("""CREATE TABLE IF NOT EXISTS peers (
                            prefix TEXT PRIMARY KEY,
                            address TEXT NOT NULL,
                            used INTEGER NOT NULL DEFAULT 0,
                            date INTEGER DEFAULT (strftime('%s', 'now')))""")
        self._db.execute("UPDATE peers SET used = 1 WHERE used = 2")
        self._db.execute("""CREATE INDEX IF NOT EXISTS
                          _peers_used ON peers(used)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS config (
                            name text primary key,
                            value text)""")
        self._db.execute('ATTACH DATABASE ":memory:" AS blacklist')
        self._db.execute("""CREATE TABLE blacklist.flag (
                            prefix TEXT PRIMARY KEY,
                            flag INTEGER NOT NULL)""")
        self._db.execute("""CREATE INDEX blacklist.blacklist_flag
                            ON flag(flag)""")
        self._db.execute("INSERT INTO blacklist.flag VALUES (?,?)", (prefix, 1))
        try:
            a, = self._db.execute("SELECT value FROM config WHERE name='registry'").next()
        except StopIteration:
            proxy = xmlrpclib.ServerProxy(registry)
            a = proxy.getPrivateAddress()
            self._db.execute("INSERT INTO config VALUES ('registry',?)", (a,))
        self._proxy = xmlrpclib.ServerProxy(a)
        logging.debug('Database prepared')

        self.next_refresh = time.time()

    def clear_blacklist(self, flag):
        logging.info('Clearing blacklist from flag %u' % flag)
        self._db.execute("DELETE FROM blacklist.flag WHERE flag = ?",
                          (flag,))
        logging.info('Blacklist cleared')

    def blacklist(self, prefix, flag):
        logging.info('Blacklisting %s' % prefix)
        self._db.execute("DELETE FROM peers WHERE prefix = ?", (prefix,))
        self._db.execute("INSERT OR REPLACE INTO blacklist.flag VALUES (?,?)",
                          (prefix, flag))
        logging.debug('%s blacklisted' % prefix)

    def whitelist(self, prefix):
        logging.info('Unblacklisting %s' % prefix)
        self._db.execute("DELETE FROM blacklist.flag WHERE prefix = ?", (prefix,))
        logging.debug('%s whitelisted' % prefix)

    def refresh(self):
        logging.info('Refreshing the peers DB...')
        try:
            self.next_refresh = time.time() + 30
            self._declare()
        except socket.error, e:
            logging.info('Connection to server failed, re-bootstraping and retrying in 30s')
        try:
            self._bootstrap()
        except socket.error, e:
            logging.debug('socket.error : %s' % e)

    def _declare(self):
        if self.address != None:
            logging.info('Sending connection info to server...')
            self._proxy.declare(utils.address_str(self.address))
            self.next_refresh = time.time() + self._refresh_time
            logging.debug('Info sent')
        else:
            logging.warning("Warning : couldn't send ip, unknown external config. retrying in 30s")

    def getUnusedPeers(self, peer_count):
        for populate in self._bootstrap, bool:
            peer_list = self._db.execute("""SELECT prefix, address FROM peers WHERE used
                                            <> 2 ORDER BY used ASC, RANDOM() LIMIT ?""",
                                         (peer_count,)).fetchall()
            if peer_list:
                return peer_list
            populate()
        logging.warning('Cannot find any new peers')
        return []

    def _bootstrap(self):
        logging.info('Getting Boot peer...')
        proxy = xmlrpclib.ServerProxy(self._registry)
        try:
            bootpeer = proxy.getBootstrapPeer(self._prefix).data
            logging.debug('Boot peer received from server')
            p = subprocess.Popen(('openssl', 'rsautl', '-decrypt', '-inkey', self._key_path),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            bootpeer = p.communicate(bootpeer)[0].split()
            return self._addPeer(bootpeer)
        except socket.error:
            pass
        except sqlite3.IntegrityError, e:
            if e.args[0] != 'column prefix is not unique':
                raise
        except Exception, e:
            logging.info('Unable to bootstrap : %s' % e)
        return False

    def usePeer(self, prefix):
        logging.trace('Updating peers database : using peer %s' % prefix)
        self._db.execute("UPDATE peers SET used = 2 WHERE prefix = ?",
                (prefix,))
        logging.debug('DB updated')

    def unusePeer(self, prefix):
        logging.trace('Updating peers database : unusing peer %s' % prefix)
        self._db.execute("UPDATE peers SET used = 1 WHERE prefix = ?",
                (prefix,))
        logging.debug('DB updated')

    def handle_message(self, msg):
        script_type, arg = msg.split()
        if script_type == 'client-connect':
            logging.info('Incoming connection from %s' % (arg,))
            prefix = utils.binFromSubnet(arg)
            if self.tunnel_manager.checkIncomingTunnel(prefix):
                self.blacklist(prefix, 2)
        elif script_type == 'client-disconnect':
            self.whitelist(utils.binFromSubnet(arg))
            logging.info('%s has disconnected' % (arg,))
        elif script_type == 'route-up':
            if not self._manual:
                external_ip = arg
                new_address = list([external_ip, port, proto]
                                   for port, proto, _ in self._pp)
                if self.address != new_address:
                    self.address = new_address
                    logging.info('Received new external ip : %s'
                              % (external_ip,))
                    try:
                        self._declare()
                    except socket.error, e:
                        logging.debug('socket.error : %s' % e)
                        logging.info("""Connection to server failed while declaring external infos""")
        else:
            logging.debug('Unknow message recieved from the openvpn pipe : %s'
                    % msg)

    def readSocket(self):
        msg = self.socket_file.readline()
        peer = msg.replace('\n', '').split(' ')
        if len(peer) != 2:
            logging.debug('Invalid package recieved : %s' % msg)
            return
        self._addPeer(peer)

    def _addPeer(self, peer):
        logging.debug('Adding peer %s' % peer)
        if int(self._db.execute("""SELECT COUNT(*) FROM blacklist.flag WHERE prefix = ?""", (peer[0],)).next()[0]) > 0:
            logging.info('Peer is blacklisted')
            return False
        self._db.execute("""DELETE FROM peers WHERE used <> 2 ORDER BY used DESC, date DESC
            LIMIT MAX(0, (SELECT COUNT(*) FROM peers
            WHERE used <> 2) - ?)""", (str(self.db_size),))
        self._db.execute("UPDATE peers SET address = ?, used = 0, date = strftime('%s','now') WHERE used = 1 and prefix = ?", (peer[1], peer[0],))
        self._db.execute("INSERT OR IGNORE INTO peers (prefix, address) VALUES (?,?)", peer)
        logging.debug('Peer added')
        return True
