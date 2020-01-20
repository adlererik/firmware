# (c) Copyright 2020 by Coinkite Inc. This file is part of Coldcard <coldcardwallet.com>
# and is covered by GPLv3 license found in COPYING.
#
# hsm.py
#
# Unattended signing of transactions and messages, subject to a set of rules.
#
import stash, ustruct, tcc, ux, chains, sys, gc, uio, ujson, uos, utime
from sffile import SFFile
from utils import problem_file_line, cleanup_deriv_path
from pincodes import AE_LONG_SECRET_LEN
from stash import blank_object
from users import Users, MAX_NUMBER_USERS
from public_constants import MAX_USERNAME_LEN
from multisig import MultisigWallet
from ubinascii import hexlify as b2a_hex
from files import CardSlot, CardMissingError

# where we save policy/config
POLICY_FNAME = '/flash/hsm-policy.json'

# number of digits in our "local confirmation" pin
LOCAL_PIN_LENGTH = 6

# max number of sats in the world: 21E6 * 1E8
MAX_SATS = const(2100000000000000)

def hsm_policy_available():
    # Is there an HSM policy ready to go? Offer the menu item then.
    try:
        uos.stat(POLICY_FNAME)
        return True
    except:
        return False

def capture_backup():
    # get a JSON-compat string to store for backup file.
    return open(POLICY_FNAME, 'rt').read()

def restore_backup(s):
    # unpack/save a our policy file from JSON-compat string
    assert s[0] == '{'
    assert s[-1] == '}'
    try:
        ujson.loads(s)

        with open(POLICY_FNAME, 'wt') as f:
            f.write(s)
    except BaseException as exc:
        # keep going, we don't want to brick
        sys.print_exception(exc)
        pass

def pop_list(j, fld_name, cleanup_fcn=None):
    # returns either None or a list of items; raises if not a list (ie. single item)
    # return [] if not defined.
    v = j.pop(fld_name, None)
    if v:
        if not isinstance(v, list):
            raise ValueError("need a list for: " + fld_name)
        if cleanup_fcn:
            return [cleanup_fcn(i) for i in v]
        return v
    else:
        return []

def pop_deriv_list(j, fld_name, extra_val=None):
    # expect a list of derivation paths, but also 'ANY' meaning accept all
    # - maybe also 'p2sh' as special value
    def cu(s):
        if s.lower() == 'any': return s.lower()
        if extra_val and s.lower() == extra_val: return s.lower()
        try:
            return cleanup_deriv_path(s)
        except:
            raise ValueError('%s: invalid path (%s)' % (fld_name, s))

    return pop_list(j, fld_name, cu)

def pop_int(j, fld_name, mn=0, mx=1000):
    # returns an int or None. Also range check.
    v = j.pop(fld_name, None)
    if v is None: return v
    assert int(v) == v, "%s: must be integer" % fld_name
    v = int(v)
    assert mn <= v <= mx, "%s: must be in range: [%d..%d]" % (fld_name, mn, mx)
    return v

def pop_bool(j, fld_name, default=False):
    # return a bool, but accept 1/0 and True/False
    return bool(j.pop(fld_name, default))

def pop_string(j, fld_name, mn_len=0, mx_len=80):
    v = j.pop(fld_name, None)
    if v is None: return v
    assert isinstance(v, str), '%s: must be string' % fld_name
    assert mn_len <= len(v) <= mx_len, '%s: length must be %d..%d' % (fld_name, mn_len, mx_len)
    return v

def assert_empty_dict(j):
    extra = set(j.keys())
    if extra:
        raise ValueError("Unknown item: " + ', '.join(extra))

def cleanup_whitelist_value(s):
    # one element in a list of addresses or paths or descriptors?
    # - later matching is string-based, so just using basic syntax check here
    # - must be checksumed-base58 or bech32
    try:
        tcc.codecs.b58_decode(s)
        return s
    except: pass

    try:
        tcc.codecs.bech32_decode(s)
        return s
    except: pass

    raise ValueError('bad whitelist value: ' + s)

class ApprovalRule:
    # A rule which describes transactions we are okay with approving. It documents:
    # - whitelist: list/pattern of destination addresses allowed (or any)
    # - per_period: velocity limit in satoshis
    # - users: list of authorized users
    # - min_users: how many of those are needed to approve
    # - local_conf: local user must also confirm w/ code

    def __init__(self, j, idx):
        # read json dict provided
        self.spent_so_far = 0       # for velocity

        def check_user(u):
            if not Users.valid_username(u):
                raise ValueError("Unknown user: %s" % u)
            return u

        self.index = idx+1
        self.per_period = pop_int(j, 'per_period', 0, MAX_SATS)
        self.max_amount = pop_int(j, 'max_amount', 0, MAX_SATS)
        self.users = pop_list(j, 'users', check_user)
        self.whitelist = pop_list(j, 'whitelist', cleanup_whitelist_value)
        self.min_users = pop_int(j, 'min_users', 1, len(self.users))
        self.local_conf = pop_bool(j, 'local_conf')
        self.wallet = pop_string(j, 'wallet', 1, 20)

        assert sorted(set(self.users)) == sorted(self.users), 'dup users'

        # usernames need to be correct and already known
        if self.min_users is None:
            self.min_users = len(self.users) if self.users else None
        else:
            # redundant w/ code in pop_int() above
            assert 1 <= self.min_users <= len(self.users), "range"

        # if specified, 'wallet' must be an existing multisig wallet's name
        if self.wallet and self.wallet != '1':
            names = [ms.name for ms in MultisigWallet.get_all()]
            assert self.wallet in names, "unknown MS wallet: "+self.wallet

        assert_empty_dict(j)

    @property
    def has_velocity(self):
        return self.per_period is not None

    def to_json(self):
        # remote users need to know what's happening, and we save this
        # cleaned up data
        flds = [ 'per_period', 'max_amount', 'users', 'min_users',
                    'local_conf', 'whitelist', 'wallet' ]
        return dict((f, getattr(self, f, None)) for f in flds)


    def to_text(self):
        # Text for humans to read and approve.
        chain = chains.current_chain()

        def render(n):
            return ' '.join(chain.render_value(n))

        if self.per_period:
            rv = 'Up to %s per period' % render(self.per_period)
            if self.max_amount:
                rv += ', and up to %s per txn' % render(self.max_amount)
        elif self.max_amount:
            rv = 'Up to %s per txn' % render(self.max_amount)
        else:
            rv = 'Any amount'

        if self.wallet == '1':
            rv += ' (non multisig)'
        elif self.wallet:
            rv += ' from multisig wallet "%s"' % self.wallet

        if self.users:
            rv += ' may be authorized by '
            if self.min_users == len(self.users) == 1:
                rv += 'user: ' + self.users[0]
            elif self.min_users == len(self.users):
                rv += 'all users: ' + ', '.join(self.users)
            elif self.min_users == 1:
                rv += 'any one user: ' + ' OR '.join(self.users)
            elif self.min_users:
                rv += 'at least %d users: ' % self.min_users
                rv += ', '.join(self.users)
        else:
            rv += ' will be approved'

        if self.whitelist:
            rv += ' provided it goes to: ' + ', '.join(self.whitelist)

        if self.local_conf:
            rv += ' if local user confirms'

        return rv

    def matches_transaction(self, psbt, users, total_out, dests, local_oked):
        # Does this rule apply to this PSBT file? 
        if self.wallet:
            # rule limited to one wallet
            if psbt.active_multisig:
                # if multisig signing, might need to match specific wallet name
                assert self.wallet == psbt.active_multisig.name, 'wrong wallet'
            else:
                # non multisig, but does this rule apply to all wallets or single-singers
                assert self.wallet == '1', 'not multisig'

        if self.max_amount is not None:
            assert total_out <= self.max_amount, 'too much out'

        # check all destinations are in the whitelist
        if self.whitelist:
            diff = set(dests) - set(self.whitelist)
            assert not diff, "non-whitelisted dest: " + ', '.join(diff)

        if self.local_conf:
            # local user must approve
            assert local_oked, "local operator didn't confirm"

        if self.users:
            # some remote users need to approve
            given = set(self.users).intersection(users)
            assert given, 'need user(s) confirmation'
            assert len(given) >= self.min_users, 'need more users to confirm (got %d of %d)'%(
                                        len(given), self.min_users)

        if self.per_period is not None:
            # check this txn would not exceed the velocity limit
            assert self.spent_so_far + total_out <= self.per_period, 'would exceed period spending'

        return True

class AuditLogger:
    def __init__(self, hsm, dirname, digest):
        self.dirname = dirname
        self.digest = digest
        self.hsm = hsm

    def __enter__(self):
        try:
            self.card = CardSlot().__enter__()

            d  = self.card.get_sd_root() + '/' + self.dirname

            # mkdir if needed
            try: uos.stat(d)
            except: uos.mkdir(d)
                
            self.fname = d + '/' + b2a_hex(self.digest[-8:]).decode('ascii') + '.log'
            self.fd = open(self.fname, 'w+t')
        except (CardMissingError, OSError):
            # may be fatal or not, depending on configuration
            self.fname = self.card = None
            self.fd = sys.stdout

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value:
            self.fd.write('\n\n---- Coldcard Exception ----\n')
            sys.print_exception(exc_value, self.fd)

        if self.card:
            assert self.fd != sys.stdout
            self.fd.close()
            self.card.__exit__(exc_type, exc_value, traceback)

    @property
    def is_unsaved(self):
        return not self.card

    def info(self, msg):
        print(msg, file=self.fd)
        if self.fd != sys.stdout:
            print(msg)

    def refuse(self, msg):
        # when things fail
        self.info("\nREFUSED: " + msg)
        self.hsm.refusals += 1
        self.hsm.last_refusal = msg

    def approve(self, msg):
        # when things fail
        self.info("\nAPPROVED: " + msg)
        self.hsm.approvals += 1
        self.hsm.last_refusal = None

class HSMPolicy:
    # implements and enforces the HSM signing/activity/logging policy
    def __init__(self):
        # no config values here.

        # statistics / state
        self.refusals = 0
        self.approvals = 0
        self.sl_reads = 0
        self.pending_auth = {}

        # velocity limits
        self.period_started = 0
        self.period_spends = {}

        self.local_code_pending = ''
        self.next_local_code = '%06d' % tcc.random.uniform(1000000)  # check vs. LOCAL_PIN_LENGTH

    

    def load(self, j):
        # Decode json object provided: destructive
        # - attr name == json name if possible
        # - NOTE: always add to self.save()!
        # - raise errors and they will be shown to user

        # fail if we can't log it
        self.must_log = pop_bool(j, 'must_log')

        # don't fail on PSBT warnings
        self.warnings_ok = pop_bool(j, 'warnings_ok')

        # a list of paths we can accept for signing
        self.msg_paths = pop_deriv_list(j, 'msg_paths')
        self.share_xpubs = pop_deriv_list(j, 'share_xpubs')
        self.share_addrs = pop_deriv_list(j, 'share_addrs', 'p2sh')

        # free text shown at top
        self.notes = j.pop('notes', None)

        # time period, in minutes
        self.period = pop_int(j, 'period', 1, 3*24*60)

        # how many times they may view the long-secret
        self.allow_sl = pop_int(j, 'allow_sl', 1, 10)

        self.set_sl = pop_string(j, 'set_sl', 16, AE_LONG_SECRET_LEN-2)
        if self.set_sl:
            assert self.allow_sl, 'need allow_sl>=1'        # because pointless otherwise

        # complex txn approval rules
        lst = pop_list(j, 'rules') or []
        self.rules = [ApprovalRule(i, idx) for idx, i in enumerate(lst)]

        if not self.period and any(i.has_velocity for i in self.rules):
            raise ValueError("Needs period to be specified")

        # error checking, must be last!
        assert_empty_dict(j)

    def period_reset_time(self):
        # time from now, in seconds, until the period resets and the velocity
        # total is reset
        if not self.period: return 0
        end = self.current_period + (self.period*60)
        return utime.time() - end
        
    def save(self):
        # create JSON document for next time.
        simple = ['must_log', 'msg_paths', 'share_xpubs', 'share_addrs',
                    'notes', 'period', 'allow_sl', 'warnings_ok']
        rv = dict()
        for fn in simple:
            rv[fn] = getattr(self, fn, None)

        rv['rules'] = [i.to_json() for i in self.rules]

        # never write this secret into JSON
        assert 'set_sl' not in rv

        return rv

    def explain(self, fd):

        if self.notes:
            fd.write('=-=\n%s\n=-=\n' % self.notes)

        fd.write('\nTransactions:\n')
        if not self.rules:
            fd.write("- No transaction will be signed.\n")
        else:
            for r in self.rules:
                fd.write('- Rule #%d: %s\n' % (r.index+1, r.to_text()))

        if self.period:
            fd.write('\nVelocity Period:\n %d minutes' % self.period)
            if self.period >= 60:
                fd.write('\n = %.3g hrs' % (self.period / 60))
            fd.write('\n')

        def plist(pl):
            remap = {'any': '(any path)', 'p2sh': '(any P2SH)' }
            return ' OR '.join(remap.get(i, i) for i in pl)

        fd.write('\nMessage signing:\n')
        if self.msg_paths:
            fd.write("- Allowed if path is: %s\n" % plist(self.msg_paths))
        else:
            fd.write("- Not allowed.\n")

        fd.write('\nOther policy:\n')
        fd.write('- MicroSD card %s receive log entries.\n' % ('MUST' if self.must_log else 'will'))
        if self.set_sl:
            fd.write('- Storage Locker will be updated (once).\n')
        if self.allow_sl:
            fd.write('- Storage Locker can be read only %s.\n' 
                        % ('once' if self.allow_sl == 1 else ('%d times' % self.allow_sl)))
        if self.warnings_ok:
            fd.write('- PSBT warnings will be ignored.\n')

        if self.share_xpubs:
            fd.write('- XPUB values will be shared, if path is: %s.\n' 
                                % plist(self.share_xpubs))
        if self.share_addrs:
            fd.write('- Address values values will be shared, if path is: %s.\n' 
                                % plist(self.share_addrs))

        self.summary = fd.getvalue()

    def status_report(self, rv):
        # Add some values we will share over USB during HSM operation
        for fn in ['summary', 'last_refusal', 'approvals', 'refusals', 'sl_reads', 'period']:
            rv[fn] = getattr(self, fn, None)

        # code the local user should enter
        rv['next_local_code'] = self.next_local_code

        # UX on web browser will need to know the local PIN code might be needed
        rv['uses_local_conf'] = any(r.local_conf for r in self.rules)

        # Velocity hints
        left = self.get_time_left()
        if (left is not None) and left >= 0:
            rv['period_ends'] = int(left+.5)
            rv['has_spent'] = [r.spent_so_far for r in self.rules]

        # sensitive values, summarize only!
        rv['pending_auth'] = len(self.pending_auth)

    def activate(self, new_file):
        # user approved activation, so apply it.
        import main
        assert not main.hsm_active
        main.hsm_active = self

        if new_file:
            # save config for next run
            with open(POLICY_FNAME, 'w+t') as f:
                ujson.dump(self.save(), f)

        # XXX not sure if I should log this
        #with AuditLogger(self, 'policy', sha) as log:
        #   log.info("Starting HSM with this policy:\n%s" % self.summary)

        if self.set_sl:
            self.save_storage_locker()

        # MAYBE: assume period has already been used up (conservative)?
        self.reset_period()

    def reset_period(self):
        # new period has begun
        for r in self.rules:
            r.spent_so_far = 0
        self.period_started = 0

    def record_spend(self, rule, amt):
        # record they spend some amount in this period
        rule.spent_so_far += amt
        if not self.period_started:
            self.period_started = utime.time() or 1

    def get_time_left(self):
        # return None if not being used, and time-left in current period if any,
        # and -1 if nothing spent yet (period hasn't started)
        # side-effect: reset if period has ended.
        if self.period is None:
            # not using feature
            return None

        if self.period_started == 0:
            # they haven't spent anything yet (in period)
            return -1

        so_far = utime.time() - self.period_started
        left = (self.period*60) - so_far
        if left <= 0:
            # period is over, reset totals
            self.reset_period()

            return -1

        return left

    def save_storage_locker(self):
        # save the "long secret" ... probably only happens first time HSM policy
        # is activated, because we don't store that original value except here 
        # and in SE.
        from main import pa

        # add length half-word to start, and pad to max size
        tmp = bytearray(AE_LONG_SECRET_LEN)
        val = self.set_sl.encode('utf8')
        ustruct.pack_into('H', tmp, 0, len(val))
        tmp[2:2+len(self.set_sl)] = val

        # write it
        pa.ls_change(tmp)

        # memory cleanup
        blank_object(tmp)
        blank_object(val)
        blank_object(self.set_sl)
        self.set_sl = None

    def fetch_storage_locker(self):
        # USB request to read the storage locker (aka. long secret from 608a)
        # - limited by counter, because typically only needed at startup
        # - please keep in mind the desktop needs this secret, and probably blabs it
        # - our memory also is contaiminated with this secret, and no easy way to clean
        assert self.allow_sl, 'not allowed'
        assert self.sl_reads < self.allow_sl, 'consumed'
        self.sl_reads += 1

        from main import pa
        raw = pa.ls_fetch()
        ll, = ustruct.unpack_from('H', raw)
        assert 0 <= ll <= AE_LONG_SECRET_LEN-2

        return raw[2:2+ll]

    def usb_auth_user(self, username, token, totp_time):
        # User via USB has proposed a totp/user/password for auth purposes
        # - but just capture data at this point, we can't use until PSBT arrives
        # - reject bogus users at this point?
        # - to avoid timing attacks, keep this linear
        assert 1 < len(username) <= MAX_USERNAME_LEN, 'badlen'
        assert len(self.pending_auth)+1 <= MAX_NUMBER_USERS, 'toomany'

        self.pending_auth[username] = (token, totp_time)

    async def approve_msg_sign(self, msg_text, address, subpath):
        # Maybe approve indicated message to be signed.
        # return 'y' or 'x'
        sha = tcc.sha256(msg_text).digest()
        with AuditLogger(self, 'messages', sha) as log:

            if self.must_log and log.is_unsaved:
                log.refuse("Could not log details, and must_log is set")
                return 'x'

            log.info('Message signing requested:')
            log.info('SHA256(msg) = ' + b2a_hex(sha).decode('ascii'))
            log.info('\n%d bytes to be signed by %s => %s' 
                            % (len(msg_text), subpath, address))

            if not self.msg_paths: 
                log.refuse("Message signing not permitted")
                return 'x'

            if 'any' not in self.msg_paths and (subpath not in self.msg_paths):
                log.refuse('Message signing not enabled for that path')
                return 'x'

            log.approve('Message signing allowed')

        return 'y'

    def approve_xpub_share(self, subpath):
        # Are we sharing XPUB read-out requests over USB?

        if not self.share_xpubs:
            return False

        if 'any' in self.share_xpubs:
            return True

        return (subpath in self.share_xpubs)

    def approve_address_share(self, subpath=None, is_p2sh=False):
        # Are we allowing "show address" requests over USB?

        if not self.share_addrs:
            return False

        if is_p2sh:
            return ('p2sh' in self.share_addrs)

        elif 'any' in self.share_addrs:
            return True

        return (subpath in self.share_addrs)

    def local_pin_entered(self, code):
        # 6 digits have been entered by local user (ie. they pressed Y, with digits in place)
        self.local_code_pending = code
        print("Got code: %s" % code)

    def consume_local_code(self):
        # Return T if they got the code right, also (regardless) pick 
        # the next code to be provided.

        expect = self.next_local_code
        got = self.local_code_pending
        self.local_code_pending = ''

        self.next_local_code = '%06d' % tcc.random.uniform(1000000)  # check vs. LOCAL_PIN_LENGTH

        return (got == expect)


    async def approve_transaction(self, psbt, psbt_sha, story):
        # Approve or don't a transaction. Catch assertions and other
        # reasons for failing/rejecting into the log.
        # - return 'y' or 'x'
        chain = chains.current_chain()
        assert psbt_sha and len(psbt_sha) == 32
        self.get_time_left()

        with AuditLogger(self, 'psbt', psbt_sha) as log:

            if self.must_log and log.is_unsaved:
                log.refuse("Could not log details, and must_log is set")
                return 'x'

            log.info('Transaction signing requested:')
            log.info('SHA256(PSBT) = ' + b2a_hex(psbt_sha).decode('ascii'))
            log.info('-vvv-\n%s\n-^^^-' % story)

            # reset pending auth list and "consume" it now
            auth = self.pending_auth
            self.pending_auth = {}

            try:
                # do this super early so always cleared even if other issues
                local_ok = self.consume_local_code()

                if not self.rules:
                    raise ValueError("no txn signing allowed")

                # reject anything with warning, probably
                if psbt.warnings:
                    if self.warnings_ok:
                        log.info("Txn has warnings, but policy is to accept anyway.")
                    else:
                        raise ValueError("has %d warning(s)" % len(psbt.warnings))

                # See who has entered creditials already (all must be valid).
                users = []
                for u, (token, counter) in auth.items():
                    problem = Users.auth_okay(u, token, totp_time=counter, psbt_hash=psbt_sha)
                    if problem:
                        log.refuse("User '%s' gave wrong auth value: %s" % (u, problem))
                        return 'x'
                    users.append(u)

                # was right code provided locally? (also resets for next attempt)
                if local_ok:
                    log.info("Local operator gave correct code.")
                if users:
                    log.info("These users gave correct auth codes: " + ', '.join(users))

                # Where is it going?
                total_out = 0
                dests = []
                for idx, tx_out in psbt.output_iter():
                    if not psbt.outputs[idx].is_change:
                        total_out += tx_out.nValue
                        dests.append(chain.render_address(tx_out.scriptPubKey))

                # Pick a rule to apply to this specific txn
                reasons = []
                for rule in self.rules:
                    try:
                        if rule.matches_transaction(psbt, users, total_out,
                                                        dests, local_ok):
                            break
                    except BaseException as exc:
                        # let's not share these details, except for debug; since
                        # they are not errors, just picking best rule in priority order
                        r = "rule #%d: %s: %s" % (rule.index+1, problem_file_line(exc), str(exc))
                        reasons.append(r)
                        print(r)
                else:
                    err = "Rejected: " + ', '.join(reasons)
                    log.refuse(err)
                    return 'x'

                if users:
                    msg = ', '.join(auth.keys())
                    if '_LOCAL' in users:
                        msg += ', and the local operator.' if msg else 'local operator'

                # looks good, do it
                log.approve("Acceptable by rule #%d" % (rule.index+1))

                if rule.per_period is not None:
                    self.record_spend(rule, total_out)

                return 'y'
            except BaseException as exc:
                sys.print_exception(exc)
                err = "Rejected: %s: %s" % (problem_file_line(exc), str(exc))
                log.refuse(err)

                return 'x'
            
def hsm_status_report():
    # Return a JSON-able object. Documented and external programs
    # rely on this output... and yet, don't overshare either.
    from auth import UserAuthorizedAction
    from main import hsm_active
    from hsm_ux import ApproveHSMPolicy

    rv = dict()
    rv['active'] = bool(hsm_active)

    if not hsm_active:
        rv['policy_available'] = hsm_policy_available()

        ar = UserAuthorizedAction.active_request
        if ar and isinstance(ar, ApproveHSMPolicy):
            # we are waiting for local user to approve entry into HSM mode
            rv['approval_wait'] = True

        # provide some keys they will need when making their policy file!
        rv['wallets'] = [ms.name for ms in MultisigWallet.get_all()]
        rv['users'] = Users.list()

    if hsm_active:
        hsm_active.status_report(rv)

    return rv
        

# EOF
