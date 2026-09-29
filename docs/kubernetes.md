# Kubernetes / k3s — evaluated, rejected

Asked in session 2: "if I make a kube cluster of Pis, will the docker deploy
there and work?"

No. Not because of effort, but because the properties k8s provides are ones a
one-DID home phone cannot use, and the properties it removes are ones SIP
depends on. Recorded here so it does not get re-proposed.

## First, literally: the test image

`test/Dockerfile` is not a runtime artifact. `ENTRYPOINT` is the test script,
Asterisk is started in the foreground by that script and torn down with the
container, nothing persists, and there is no readiness or liveness surface.
Deploying it would give you a pod that runs a test suite and exits. A real
image is maybe 30 lines — that is not the obstacle.

## The five things that actually break

### 1. RTP does not fit through a Service

Media needs **10000-20000/udp**, ten thousand ports. The default NodePort
range is 30000-32767 and is meant for a handful of ports, not a block that
size. You can widen it with `--service-node-port-range`, but every port
becomes an iptables/IPVS DNAT rule; kube-proxy rule count is already the
usual scaling complaint at a few thousand services.

The standard escape is `hostNetwork: true`. That works — and it deletes the
reason to use k8s. The pod binds the node's interface directly: no Service,
no ClusterIP, no kube-proxy, no network policy. You have a container pinned
to one machine's network stack, which is a systemd unit with extra steps.

### 2. SIP puts IP addresses inside the payload

kube-proxy rewrites L3/L4 headers. SIP advertises its contact address and
media address in the **SDP body**, at L7. The pod announces its cluster IP,
which is not routable from your LAN or from voip.ms, so signalling succeeds
and audio goes nowhere — one-way or dead.

`external_media_address` / `external_signaling_address` in `pjsip.conf` exist
for exactly this, but they need a **stable, known** address. A rescheduled pod
gets a new IP. You would be pinning the pod anyway (see 1) to have something
stable to write down.

### 3. astdb has no clustering story

Everything stateful in this project lives in astdb — the name book, `allow/`
(which **self-learns**: press 1 once, whitelisted forever), `block/`, and the
Alexa sensor map. It is a local sqlite file under `/var/lib/asterisk`, with
no replication and no leader election.

Two replicas means two divergent databases. A caller whitelisted on pod A is
still gated on pod B. Add `/var/lib/orata/alexa.token` and the voicemail
spool (plain files) and you need RWX persistent storage — NFS or Longhorn on
Pis, another moving part with its own failure modes, in front of a workload
whose entire state would fit in a text file.

### 4. You cannot load-balance a SIP registration

The trunk works by registering **outbound** to voip.ms; inbound calls ride
that registration. This is why no port is forwarded, and it is a security
invariant in HANDOFF.

A registration is a single binding. Two Asterisk instances registering the
same sub-account overwrite each other — last writer wins, and inbound calls
go to whichever re-registered most recently, flapping on every expiry. That
is worse than one instance, not better.

Fixing it properly means a SIP proxy (Kamailio/OpenSIPS) in front with a
shared registrar DB, which is the real pattern for clustered telephony and is
an order of magnitude more machinery than the thing it would be fronting.

### 5. Self-healing does not heal a call

A call is a long-lived stateful session with an RTP stream. If the pod dies
mid-call, the call drops. k8s restarts the pod; it cannot migrate the media
stream, and there is no state to resume into. Your "HA" telephony system
still drops every in-progress call on failover.

What you would gain is faster recovery of the *ability to receive new calls*
— from maybe 30s (systemd `Restart=on-failure`) to maybe 20s (reschedule +
image pull + Asterisk start + re-register). For a home phone that is
indistinguishable.

## What survives

If you already run a cluster for other things, the right answer is to run
orata **outside** it on a dedicated Pi. Telephony wants a stable IP, stable
ports, and local disk. That is the least cloud-native workload in your house
and the clearest candidate for staying on metal.

The pieces that *are* cluster-shaped, if you ever want them:

- `orata-web.py` — stateless except for reading astdb via the Asterisk CLI,
  which is a local unix socket, so it must be co-located anyway. Not worth
  splitting.
- ntfy, if you self-host it instead of using ntfy.sh. Genuinely stateless
  HTTP, a fine cluster workload, and entirely independent of Asterisk.

## If you want to do it anyway

It is your cluster. The honest minimum:

```yaml
# sketch, not tested
spec:
  replicas: 1                      # more than 1 is actively harmful, see 3 & 4
  template:
    spec:
      hostNetwork: true            # mandatory; forfeits k8s networking
      dnsPolicy: ClusterFirstWithHostNet
      nodeSelector:
        kubernetes.io/hostname: pi-phone   # pin it; astdb is node-local
      containers:
        - name: asterisk
          volumeMounts:
            - { name: astdb,  mountPath: /var/lib/asterisk }
            - { name: spool,  mountPath: /var/spool/asterisk }
            - { name: orata,  mountPath: /var/lib/orata }
      volumes:
        - name: astdb
          hostPath: { path: /srv/orata/astdb, type: DirectoryOrCreate }
        # ...local-path PVs are equivalent here and equally node-bound
```

Read that back: one replica, host networking, pinned to a named node,
node-local storage. Every k8s feature is switched off. The result is Asterisk
on one specific Pi, with a control plane and a YAML file interposed between
you and it.

The one real benefit is declarative image versioning and rollback. Weigh that
against a second daemon (k3s) that holds your phone line hostage to its own
upgrade cadence — precisely the objection that killed FreePBX and Home
Assistant.

## Verdict

Same shape as the Docker decision: rejected for runtime, useful for testing.
If you want cluster practice, pick a workload that is stateless and does not
embed IP addresses in its payload. Nearly anything qualifies; SIP is close to
the worst case.