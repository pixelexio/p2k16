import logging
import sys

from ldaptor.entry import BaseLDAPEntry
from ldaptor.interfaces import IConnectedLDAPEntry
from ldaptor.protocols.ldap import ldaperrors
from ldaptor.protocols.ldap.distinguishedname import DistinguishedName, RelativeDistinguishedName
from ldaptor.protocols.ldap.distinguishedname import LDAPAttributeTypeAndValue
from ldaptor.protocols.ldap.ldapserver import LDAPServer
from twisted.application import service
from twisted.internet import defer
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.internet.endpoints import serverFromString
from twisted.internet.protocol import ServerFactory
from twisted.python import log
from twisted.python.components import registerAdapter
from twisted.python.failure import Failure
from txpostgres import txpostgres, reconnection

from p2k16.core import crypto

logger = logging.getLogger(__name__)


class LoggingDetector(reconnection.DeadConnectionDetector):

    def startReconnecting(self, f):
        logger.warning("Database connection is down (error: {})".format(f.value))
        return reconnection.DeadConnectionDetector.startReconnecting(self, f)

    def reconnect(self):
        logger.warning("Database reconnecting...")
        return reconnection.DeadConnectionDetector.reconnect(self)

    def connectionRecovered(self):
        logger.warning("Database connection recovered")
        return reconnection.DeadConnectionDetector.connectionRecovered(self)


con = None  # type: txpostgres.Connection


def configure_db():
    def connection_error(f):
        logger.info("Database connection failed with {}".format(f))

    def connected(_, _con):
        global con
        con = _con
        logger.info("Database connected")

    conn = txpostgres.Connection(detector=LoggingDetector())
    d = conn.connect('dbname=p2k16')

    # if the connection failed, log the error and start reconnecting
    d.addErrback(conn.detector.checkForDeadConnection)
    d.addErrback(connection_error)
    d.addCallback(connected, conn)


class Account(BaseLDAPEntry):
    def __init__(self, dn, attrs=None):
        super().__init__(dn, attrs or {})
        # self.dn = DistinguishedName(dn)
        # self.attrs = attrs or {}

    def _bind(self, password):
        password = password.decode("utf-8")
        log.msg("Account.bind: password={}".format(password))

        for key in self._user_password_keys:
            for digest in self.get(key, []):
                log.msg("Account.bind: userPassword={}".format(digest))
                if crypto.check_password(digest, password):
                    return self
        raise ldaperrors.LDAPInvalidCredentials()


def _create_account(row, people_dn):
    base = list(people_dn.listOfRDNs)
    (username, name, email, password) = row
    attrs = {"uid": [username]}

    if name is not None:
        attrs["displayName"] = [name]

    if email is not None:
        attrs["mail"] = [email]

    if password is not None:
        attrs["userPassword"] = [password]

    av = LDAPAttributeTypeAndValue(attributeType="uid", value=username)
    dn = [RelativeDistinguishedName(attributeTypesAndValues=[av])] + base
    return Account(dn, attrs)


@inlineCallbacks
def lookup_account(username, bound_dn: Account, people_dn):
    log.msg("bound_dn={}".format(bound_dn.toWire() if bound_dn is not None else ""))
    res = yield con.runQuery("select username, name, email, password from account where username=%s", [username])

    if len(res) == 1:
        return _create_account(res[0], people_dn)
    return None


@inlineCallbacks
def search_account(bound_dn: Account, people_dn):
    log.msg("bound_dn={}".format(bound_dn.toWire() if bound_dn is not None else ""))
    res = yield con.runQuery("select username, name, email, NULL as password from account")

    return [_create_account(row, people_dn) for row in res]


class Forest(object):
    def __init__(self, ldap_server):
        self.ldap_server = ldap_server
        self.roots = {}

    def bound_user(self):
        return self.ldap_server.boundUser

    def add_tree(self, tree: "Tree"):
        self.roots[tree.mount_point] = tree

    def lookup(self, dn: DistinguishedName):
        for tree in self.roots.values():  # type: Tree
            if tree.mount_point.contains(dn):
                return tree.lookup(self, dn)

        return defer.fail()


class Tree(object):
    def __init__(self, mount_point, con):
        self.mount_point = mount_point  # type: DistinguishedName
        self.people_dn = DistinguishedName([RelativeDistinguishedName("ou=People")] + list(mount_point.listOfRDNs))
        self.con = con

    @inlineCallbacks
    def lookup(self, context, dn: DistinguishedName, *args):
        log.msg("lookup: dn={}, args={}".format(dn.toWire(), args))
        # log.msg("rdn={}".format(dn.listOfRDNs))
        # log.msg("dn.up={}".format(dn.up().toWire()))

        if dn == self.people_dn:
            return Focus(self, context, dn)

        if dn.up() == self.people_dn:
            kv = dn.listOfRDNs[0].split()[0]  # type: LDAPAttributeTypeAndValue
            (field, name) = (kv.attributeType, kv.value)

            if field == "uid":
                account = yield lookup_account(name, context.bound_user(), self.people_dn)
                if account is not None:
                    # TODO: check password
                    # ldaperrors.LDAPInvalidCredentials()
                    return account

                return None
            else:
                return None

        raise ldaperrors.LDAPProtocolError("lookup!!")


class Focus(object):
    def __init__(self, tree: Tree, context, base_dn: DistinguishedName):
        self.tree = tree
        self.context = context
        self.base_dn = base_dn

    @inlineCallbacks
    def search(self, filterObject, attributes, scope, derefAliases, sizeLimit, timeLimit, typesOnly, callback, *args,
               **kwargs):
        log.msg("Focus.search: args={}, kwargs={}".format(args, kwargs))
        # log.msg("filterObject={}".format([filterObject]))
        # log.msg("attributes={}".format(attributes))
        # log.msg("scope={}".format(scope))
        # log.msg("derefAliases={}".format(derefAliases))
        # log.msg("sizeLimit={}".format(sizeLimit))
        # log.msg("timeLimit={}".format(timeLimit))
        # log.msg("typesOnly={}".format(typesOnly))

        accounts = yield search_account(self.context.bound_user(), self.tree.people_dn)
        for account in accounts:
            callback(account)


class LDAPServerFactory(ServerFactory):
    protocol = LDAPServer

    def __init__(self):
        self.current_proto = None

    def buildProtocol(self, addr):
        proto = self.protocol()
        proto.debug = self.debug
        proto.factory = self
        self.current_proto = proto
        return proto


def run_ldap_server(ldap_port, ldaps_port, ldaps_cert, ldaps_key):
    # Configure logging
    logging_observer = log.PythonLoggingObserver()
    logging_observer.start()

    base_dn = DistinguishedName(stringValue="dc=bitraf,dc=no")

    def make_forest(factory):
        forest = Forest(factory.current_proto)
        forest.add_tree(Tree(base_dn, con))
        return forest

    registerAdapter(make_forest, LDAPServerFactory, IConnectedLDAPEntry)
    factory = LDAPServerFactory()
    factory.debug = True
    application = service.Application("ldaptor-server")
    my_service = service.IServiceCollection(application)
    e = serverFromString(reactor, "tcp:{0}".format(ldap_port))
    d = e.listen(factory)

    def server_started(*args):
        # logger.info("Launching LDAP server. LDAP port: {}, LDAPS port: {}".format(ldap_port, ldaps_port))
        configure_db()
        reactor.run()

    def server_failed(x: Failure):
        print(x.value, file=sys.stderr)

    d.addCallback(server_started)
    d.addErrback(server_failed)