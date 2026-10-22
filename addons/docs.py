"""
addons.docs - hover help for the "Networking & Storage" mode (applied by field_docs.apply_addons).
A[type][key] / A[type]["table.column"] = (description, maps_to, {option: explanation}).
CSI types share CSI_COMMON; CSI driver parameters fall back to "StorageClass parameters.<name>".
"""
from __future__ import annotations

ED = {"": "operator default", "Enabled": "turn the feature on", "Disabled": "turn the feature off"}
VERSION = "Release that install.sh installs (pinned, verified to exist Oct 2026). Type another release to pin it."

A: dict[str, dict] = {}

A["Calico"] = {
    "cal_version": (f"Calico {VERSION} Used for the operator-crds.yaml and tigera-operator.yaml URLs.",
                    "install.sh: .../calico/<version>/manifests/tigera-operator.yaml", {}),
    "cal_provider": ("Kubernetes distribution Calico runs on; the operator adapts defaults (MTU, CNI paths, "
                     "IPAM) to it.", "Installation.spec.kubernetesProvider",
                     {"": "auto-detect (fine for kubeadm / vanilla)", "Kind": "kind clusters",
                      "EKS": "Amazon EKS (usually policy-only alongside the VPC CNI)", "GKE": "Google GKE",
                      "AKS": "Azure AKS (bring-your-own CNI)", "OpenShift": "Red Hat OpenShift",
                      "RKE2": "Rancher RKE2 with cni: none", "TKG": "VMware Tanzu Kubernetes Grid",
                      "DockerEnterprise": "Mirantis / Docker Enterprise"}),
    "cal_wait": ("When ticked, install.sh waits for the tigerastatus 'calico' object to become Available "
                 "before it exits, so nodes are Ready when it returns.", "install.sh: kubectl wait tigerastatus/calico", {}),
    "cal_pools": ("Pod address ranges Calico allocates from. The first pool's CIDR must match the cluster's pod "
                  "subnet (kubeadm podSubnet, kind podSubnet, k3s cluster-cidr).",
                  "Installation.spec.calicoNetwork.ipPools[]", {}),
    "cal_pools.name": ("Name of the IPPool object the operator creates. Example: default-ipv4-ippool",
                       "ipPools[].name", {}),
    "cal_pools.cidr": ("Address range of the pool; must sit inside the cluster's pod CIDR. Example: 10.244.0.0/16",
                       "ipPools[].cidr", {}),
    "cal_pools.encapsulation": ("How pod traffic crosses nodes.", "ipPools[].encapsulation",
                                {"VXLANCrossSubnet": "VXLAN only between subnets, plain routing inside one (default)",
                                 "VXLAN": "always VXLAN; works everywhere, no BGP needed",
                                 "IPIP": "IP-in-IP; needs BGP Enabled", "IPIPCrossSubnet": "IP-in-IP only across subnets; needs BGP",
                                 "None": "no overlay: the network must route pod CIDRs (BGP Enabled)"}),
    "cal_pools.nat_outgoing": ("Masquerade traffic leaving the pool to addresses outside all pools.",
                               "ipPools[].natOutgoing", {"Enabled": "pods reach the internet via the node IP (usual)",
                                                         "Disabled": "pod IPs are routable outside the cluster"}),
    "cal_pools.block_size": ("Prefix length of the per-node blocks carved from the pool; blank = 26 (64 IPs). "
                             "IPv4 20-32. Example: 26", "ipPools[].blockSize", {}),
    "cal_pools.node_selector": ("Which nodes allocate from this pool (Calico selector syntax); blank = all(). "
                                "Example: zone == 'a'", "ipPools[].nodeSelector", {}),
    "cal_bgp": ("Whether Calico runs BGP (BIRD) to distribute routes.", "Installation.spec.calicoNetwork.bgp",
                {"": "operator default (Enabled unless all pools are VXLAN)", "Enabled": "run BGP; needed for IPIP / None",
                 "Disabled": "VXLAN-only, no BGP peering (simplest on clouds / kind)"}),
    "cal_mtu": ("MTU of pod interfaces; blank lets Calico auto-detect from the host interface minus overlay "
                "overhead. Example: 1450", "Installation.spec.calicoNetwork.mtu", {}),
    "cal_dataplane": ("Packet-processing engine on Linux nodes.", "Installation.spec.calicoNetwork.linuxDataplane",
                      {"": "operator default (Iptables)", "Iptables": "classic iptables rules",
                       "Nftables": "nftables rules (newer kernels)",
                       "BPF": "eBPF dataplane; replaces kube-proxy - set the API server host below"}),
    "cal_host_ports": ("Support for hostPort in pod specs via the portmap plugin.",
                       "Installation.spec.calicoNetwork.hostPorts", ED),
    "cal_autodetect": ("How calico-node picks each node's IPv4 address for BGP / VXLAN.",
                       "Installation.spec.calicoNetwork.nodeAddressAutodetectionV4",
                       {"": "operator default (firstFound)", "firstFound": "first valid interface address",
                        "kubernetes": "the Node object's InternalIP", "interface": "interfaces matching a regex",
                        "skipInterface": "skip interfaces matching a regex", "canReach": "address used to reach a host",
                        "cidrs": "address inside the listed CIDRs"}),
    "cal_autodetect_value": ("Argument of the autodetection method: a regex for interface / skipInterface, a "
                             "host for canReach, comma-separated CIDRs for cidrs. Example: eth.*",
                             "nodeAddressAutodetectionV4.<method>", {}),
    "cal_kube_proxy_mgmt": ("Let the operator disable kube-proxy when the BPF dataplane is active.",
                            "Installation.spec.calicoNetwork.kubeProxyManagement", ED),
    "cal_bpf_bootstrap": ("Let the operator find the API server endpoint itself for the BPF dataplane (no "
                          "kubernetes-services-endpoint ConfigMap needed).",
                          "Installation.spec.calicoNetwork.bpfNetworkBootstrap", ED),
    "cal_api_host": ("Real address of the API server (not the kubernetes Service ClusterIP). Needed by eBPF mode; "
                     "written to the kubernetes-services-endpoint ConfigMap. Example: 172.18.0.2",
                     "ConfigMap tigera-operator/kubernetes-services-endpoint: KUBERNETES_SERVICE_HOST", {}),
    "cal_api_port": ("Port of the API server address above. Example: 6443",
                     "kubernetes-services-endpoint: KUBERNETES_SERVICE_PORT", {}),
    "cal_apiserver": ("Create the APIServer CR so projectcalico.org/v3 resources (GlobalNetworkPolicy...) work "
                      "with kubectl.", "custom-resources.yaml: kind APIServer", {}),
    "cal_goldmane": ("Create the Goldmane CR: the flow-log aggregator that feeds Whisker.",
                     "custom-resources.yaml: kind Goldmane", {}),
    "cal_whisker": ("Create the Whisker CR: the web UI that shows network flows and policy verdicts "
                    "(needs Goldmane).", "custom-resources.yaml: kind Whisker", {}),
}

A["Cilium"] = {
    "cil_version": (f"Cilium Helm chart {VERSION}", "helm --version / cilium install --version", {}),
    "cil_method": ("Tool install.sh uses; both read the same values.yaml.", "install.sh",
                   {"helm": "helm upgrade --install cilium cilium/cilium -f values.yaml",
                    "cilium CLI": "cilium install -f values.yaml (+ cilium status --wait)"}),
    "cil_namespace": ("Namespace for the Cilium agent and operator. Example: kube-system",
                      "helm --namespace", {}),
    "cil_ipam": ("Where pod IPs come from.", "values.yaml: ipam.mode",
                 {"cluster-pool": "Cilium operator hands out per-node CIDRs from the list below (default)",
                  "kubernetes": "use each Node's spec.podCIDR (kube-controller-manager allocates)",
                  "eni": "AWS ENI IPs (VPC-routable); native routing", "azure": "Azure IPAM; native routing"}),
    "cil_pool_cidrs": ("Pod CIDRs for cluster-pool IPAM, comma-separated. Should match the cluster's pod subnet "
                       "to avoid overlaps. Example: 10.244.0.0/16",
                       "values.yaml: ipam.operator.clusterPoolIPv4PodCIDRList", {}),
    "cil_pool_mask": ("Prefix length of the per-node CIDR cut from the pool. Example: 24 (254 pods per node)",
                      "values.yaml: ipam.operator.clusterPoolIPv4MaskSize", {}),
    "cil_kpr": ("Replace kube-proxy with Cilium's eBPF service handling.", "values.yaml: kubeProxyReplacement",
                {"false": "keep kube-proxy", "true": "eBPF replaces kube-proxy; needs k8sServiceHost and no "
                 "kube-proxy (kind: kubeProxyMode none)"}),
    "cil_api_host": ("API server address the agent uses when kube-proxy is absent; 'auto' reads it from the "
                     "cluster-info ConfigMap. Example: 172.18.0.2", "values.yaml: k8sServiceHost", {}),
    "cil_api_port": ("Port of k8sServiceHost (the API server port, not the Service port). Example: 6443", "values.yaml: k8sServicePort", {}),
    "cil_routing": ("How pod traffic crosses nodes.", "values.yaml: routingMode",
                    {"tunnel": "encapsulate (VXLAN / Geneve); works on any network",
                     "native": "no overlay: the network routes pod CIDRs (set ipv4NativeRoutingCIDR)"}),
    "cil_tunnel": ("Encapsulation for tunnel mode.", "values.yaml: tunnelProtocol",
                   {"vxlan": "UDP 8472, widely supported", "geneve": "UDP 6081, needed for some features (DSR with Geneve)"}),
    "cil_native_cidr": ("CIDR that is routable without masquerading in native mode, usually the pod CIDR. "
                        "Example: 10.244.0.0/16", "values.yaml: ipv4NativeRoutingCIDR", {}),
    "cil_auto_routes": ("Install direct routes to other nodes' pod CIDRs (native mode, nodes on one L2 "
                        "segment).", "values.yaml: autoDirectNodeRoutes", {}),
    "cil_bpf_masq": ("Masquerade in eBPF instead of iptables (needs kubeProxyReplacement).",
                     "values.yaml: bpf.masquerade", {}),
    "cil_mtu": ("MTU for pod interfaces; blank = auto-detect. Example: 1450", "values.yaml: MTU", {}),
    "cil_hubble": ("Enable Hubble flow visibility inside the agents (hubble observe).",
                   "values.yaml: hubble.enabled", {}),
    "cil_relay": ("Deploy Hubble Relay, the cluster-wide flow API used by the CLI and UI.",
                  "values.yaml: hubble.relay.enabled", {}),
    "cil_ui": ("Deploy the Hubble UI web console (needs Relay); open with cilium hubble ui.",
               "values.yaml: hubble.ui.enabled", {}),
    "cil_encryption": ("Encrypt pod-to-pod traffic between nodes.", "values.yaml: encryption.enabled / type",
                       {"none": "no transparent encryption", "wireguard": "WireGuard (simple, kernel >= 5.6)",
                        "ipsec": "IPsec; install.sh creates the cilium-ipsec-keys Secret with a random key"}),
    "cil_node_encryption": ("Also encrypt node-to-node (host) traffic; WireGuard only.",
                            "values.yaml: encryption.nodeEncryption", {}),
    "cil_policy_mode": ("When endpoints enforce policy.", "values.yaml: policyEnforcementMode",
                        {"default": "deny only once a policy selects the endpoint (Kubernetes semantics)",
                         "always": "deny everything not explicitly allowed", "never": "disable policy enforcement"}),
    "cil_bgp": ("Enable the BGP control plane (CiliumBGPClusterConfig etc.) to advertise pod / Service "
                "routes.", "values.yaml: bgpControlPlane.enabled", {}),
    "cil_gateway": ("Cilium implements Gateway API; install.sh installs the Gateway API CRDs first. Needs "
                    "kubeProxyReplacement.", "values.yaml: gatewayAPI.enabled", {}),
    "cil_l2": ("Answer ARP for LoadBalancer IPs (L2 announcements, MetalLB-style). Needs "
               "kubeProxyReplacement.", "values.yaml: l2announcements.enabled", {}),
    "cil_operator_replicas": ("Number of cilium-operator replicas; blank = chart default 2. Use 1 on single-node "
                              "clusters. Example: 1", "values.yaml: operator.replicas", {}),
}

A["Flannel"] = {
    "fl_version": (f"Flannel {VERSION} install.sh applies that release's kube-flannel.yml.",
                   "install.sh: .../flannel/releases/download/<version>/kube-flannel.yml", {}),
    "fl_iface": ("Interface (or IP) flannel uses for inter-node traffic; blank = the default-route interface. "
                 "install.sh patches the DaemonSet args. Example: eth1", "kube-flannel DaemonSet: --iface", {}),
    "fl_network": ("Cluster-wide pod CIDR; must equal the cluster's pod subnet (kubeadm podSubnet / "
                   "--cluster-cidr). Example: 10.244.0.0/16", "net-conf.json: Network", {}),
    "fl_subnet_len": ("Prefix length of each node's subnet; blank = 24. Example: 24", "net-conf.json: SubnetLen", {}),
    "fl_ipv6": ("Enable IPv6 (dual-stack) pod networking; set IPv6Network too.", "net-conf.json: EnableIPv6", {}),
    "fl_ipv6_network": ("IPv6 pod CIDR when EnableIPv6 is ticked. Example: fd00:10:244::/56",
                        "net-conf.json: IPv6Network", {}),
    "fl_nftables": ("Use nftables instead of iptables for masquerading (newer distributions).",
                    "net-conf.json: EnableNFTables", {}),
    "fl_backend": ("How flannel carries pod traffic between nodes.", "net-conf.json: Backend.Type",
                   {"vxlan": "VXLAN overlay (default, works everywhere)",
                    "host-gw": "plain routes via node IPs; nodes must share an L2 network",
                    "wireguard": "encrypted WireGuard tunnels", "udp": "userspace UDP (debug only, slow)",
                    "ipip": "IP-in-IP kernel tunnels", "ipsec": "IPsec (strongSwan) encrypted tunnels"}),
    "fl_vni": ("VXLAN network identifier; blank = 1. Example: 1", "net-conf.json: Backend.VNI", {}),
    "fl_port": ("UDP port for VXLAN; blank = kernel default 8472. Example: 8472", "net-conf.json: Backend.Port", {}),
    "fl_direct_routing": ("Use direct routes when nodes share a subnet and encapsulate only across subnets.",
                          "net-conf.json: Backend.DirectRouting", {}),
    "fl_wg_keepalive": ("WireGuard keepalive in seconds for nodes behind NAT; blank = off. Example: 25",
                        "net-conf.json: Backend.PersistentKeepaliveInterval", {}),
}

A["Canal"] = {
    "cn_version": (f"Calico release whose canal.yaml (Calico policy + flannel networking) is installed. {VERSION}",
                   "install.sh: .../calico/<version>/manifests/canal.yaml", {}),
    "cn_network": ("Pod CIDR for flannel; must equal the cluster's pod subnet. Example: 10.244.0.0/16",
                   "canal-config: net-conf.json Network", {}),
    "cn_backend": ("Flannel backend inside Canal.", "canal-config: net-conf.json Backend.Type",
                   {"vxlan": "VXLAN overlay (default)", "host-gw": "direct routes; nodes on one L2 network"}),
    "cn_iface": ("Interface flannel uses between nodes; blank = the default-route interface. Example: eth1",
                 "canal-config: canal_iface", {}),
    "cn_masquerade": ("Masquerade pod traffic leaving the pod network (usual).", "canal-config: masquerade", {}),
    "cn_mtu": ("MTU of pod veths; 0 = auto-detect. Example: 1450", "canal-config: veth_mtu", {}),
}

A["Antrea"] = {
    "an_version": (f"Antrea Helm chart {VERSION}", "helm --version (antrea/antrea)", {}),
    "an_encap": ("How pod traffic crosses nodes.", "values.yaml: trafficEncapMode",
                 {"encap": "always tunnel (default)", "noEncap": "routed; the network must route pod CIDRs",
                  "hybrid": "route inside a subnet, tunnel across subnets",
                  "networkPolicyOnly": "policy only, chained with a cloud CNI (EKS / AKS / GKE)"}),
    "an_tunnel": ("Tunnel protocol in encap / hybrid mode.", "values.yaml: tunnelType",
                  {"geneve": "default, UDP 6081", "vxlan": "UDP 4789", "gre": "IP protocol 47 (no IPv6)"}),
    "an_encryption": ("Encrypt traffic between nodes.", "values.yaml: trafficEncryptionMode",
                      {"none": "no encryption", "ipsec": "IPsec; install.sh generates a random PSK",
                       "wireguard": "WireGuard on the port below"}),
    "an_wg_port": ("UDP port for WireGuard encryption. Example: 51820", "values.yaml: wireGuard.port", {}),
    "an_mtu": ("MTU of pod interfaces; blank = auto. Example: 1450", "values.yaml: defaultMTU", {}),
    "an_no_snat": ("Don't masquerade egress pod traffic (pod IPs routable outside).", "values.yaml: noSNAT", {}),
    "an_transport_iface": ("Interface for tunnel / routed traffic; blank = the interface owning the node IP. "
                           "Example: eth1", "values.yaml: transportInterface", {}),
    "an_proxy_all": ("Antrea Proxy handles all Service traffic so kube-proxy can be removed; set "
                     "kubeAPIServerOverride.", "values.yaml: antreaProxy.proxyAll", {}),
    "an_api_override": ("API server host:port the agent uses without kube-proxy. Example: 172.18.0.2:6443",
                        "values.yaml: kubeAPIServerOverride", {}),
    "an_node_ipam": ("Let the Antrea controller allocate node pod CIDRs (when kube-controller-manager doesn't).",
                     "values.yaml: nodeIPAM.enable", {}),
    "an_cluster_cidrs": ("Pod CIDRs for Antrea NodeIPAM, comma-separated. Example: 10.244.0.0/16",
                         "values.yaml: nodeIPAM.clusterCIDRs", {}),
    "an_feature_gates": ("Antrea feature gates, one Name=true|false per line. Example: Multicast=true",
                         "values.yaml: featureGates", {}),
}

A["Multus"] = {
    "mu_version": (f"Multus {VERSION}", "install.sh: .../multus-cni/<version>/deployments/...", {}),
    "mu_flavor": ("Which Multus DaemonSet to deploy.", "install.sh: multus-daemonset(-thick).yml",
                  {"thick": "client/server design with a daemon; recommended", "thin": "classic single binary shim"}),
    "mu_whereabouts": ("Also install Whereabouts, a cluster-wide IPAM for secondary networks (needed for the "
                       "whereabouts IPAM type).", "install.sh: whereabouts daemonset + CRDs", {}),
    "mu_nad_name": ("Name of the NetworkAttachmentDefinition; pods reference it in the "
                    "k8s.v1.cni.cncf.io/networks annotation. Example: macvlan-conf", "metadata.name", {}),
    "mu_nad_namespace": ("Namespace of the NetworkAttachmentDefinition; pods in other namespaces reference "
                         "it as <ns>/<name>. Example: default", "metadata.namespace", {}),
    "mu_type": ("CNI plugin that creates the secondary interface.", "spec.config: type",
                {"macvlan": "new MAC on a host interface (L2 presence on the LAN)",
                 "ipvlan": "shares the host MAC (for networks that limit MACs)",
                 "bridge": "Linux bridge on the node", "host-device": "move a whole host NIC / VF into the pod"}),
    "mu_master": ("Host interface macvlan / ipvlan attach to; blank = default-route interface. Example: eth0",
                  "spec.config: master", {}),
    "mu_macvlan_mode": ("macvlan mode.", "spec.config: mode",
                        {"bridge": "sub-interfaces can talk to each other (default)", "private": "isolate sub-interfaces",
                         "vepa": "hairpin via the external switch", "passthru": "single sub-interface owns the NIC"}),
    "mu_ipvlan_mode": ("ipvlan mode.", "spec.config: mode",
                       {"l2": "switching (default)", "l3": "routing, no broadcast", "l3s": "l3 with iptables conntrack"}),
    "mu_bridge": ("Name of the Linux bridge on each node (created if missing). Example: br0", "spec.config: bridge", {}),
    "mu_is_gateway": ("Assign the gateway IP to the bridge so pods can route through it.",
                      "spec.config: isGateway", {}),
    "mu_ip_masq": ("Masquerade traffic leaving the bridge network.", "spec.config: ipMasq", {}),
    "mu_device": ("Host device moved into the pod: interface name, MAC or PCI address. Example: eth1",
                  "spec.config: device", {}),
    "mu_mtu": ("MTU of the secondary interface; blank = plugin default. Example: 9000", "spec.config: mtu", {}),
    "mu_ipam": ("How the secondary interface gets its address.", "spec.config: ipam.type",
                {"host-local": "per-node range (addresses may repeat across nodes)",
                 "whereabouts": "cluster-wide unique allocation (install Whereabouts)",
                 "static": "fixed addresses listed below", "dhcp": "DHCP on the attached network (needs the dhcp daemon)",
                 "none": "no IPAM (L2 only)"}),
    "mu_subnet": ("Subnet of the secondary network (whereabouts: range). Example: 192.168.1.0/24",
                  "spec.config: ipam.subnet / ipam.range", {}),
    "mu_range_start": ("First address to hand out; blank = start of the subnet. Example: 192.168.1.200",
                       "spec.config: ipam.rangeStart / range_start", {}),
    "mu_range_end": ("Last address to hand out; blank = end of the subnet. Example: 192.168.1.216",
                     "spec.config: ipam.rangeEnd / range_end", {}),
    "mu_gateway": ("Gateway address on the secondary network; blank = none. Example: 192.168.1.1",
                   "spec.config: ipam.gateway", {}),
    "mu_routes": ("Extra routes via the secondary interface, one 'dst [via gw]' per line. Example: 10.10.0.0/16",
                  "spec.config: ipam.routes[]", {}),
    "mu_exclude": ("Addresses whereabouts must never hand out, comma-separated CIDRs. Example: 192.168.1.200/30",
                   "spec.config: ipam.exclude[]", {}),
    "mu_static": ("Fixed addresses for static IPAM, one 'ip/prefix [gateway]' per line. Example: "
                  "192.168.1.50/24 192.168.1.1", "spec.config: ipam.addresses[]", {}),
    "mu_example": ("Also write example-pod.yaml: a busybox pod that requests this network so you can check "
                   "the extra interface (ip addr).", "example-pod.yaml", {}),
}

FS = {"ext4": "ext4 (default for most drivers)", "xfs": "XFS (large files, databases)", "ext3": "legacy ext3",
      "btrfs": "btrfs (snapshots / compression)"}
ONDEL = {"delete": "remove the volume's directory when the PV is deleted (default)",
         "retain": "keep the directory on the server", "archive": "rename it to archived-<name> instead of deleting"}

CSI_COMMON = {
    "name": ("Name of the StorageClass; PVCs request it with storageClassName. Shows up in the PVC / "
             "StatefulSet storageClass dropdowns of this session. Example: fast", "metadata.name", {}),
    "default_class": ("Mark this as the cluster's default StorageClass (used by PVCs without storageClassName). "
                      "Only one class should be default.",
                      "metadata.annotations: storageclass.kubernetes.io/is-default-class", {}),
    "reclaim_policy": ("What happens to the volume when its PVC is deleted.", "reclaimPolicy",
                       {"Delete": "delete the backing volume with the PVC (default)",
                        "Retain": "keep the volume; an admin must clean up / re-bind the PV"}),
    "binding_mode": ("When volumes are provisioned.", "volumeBindingMode",
                     {"WaitForFirstConsumer": "wait for the pod to be scheduled, so the volume lands in its zone / node",
                      "Immediate": "provision as soon as the PVC is created"}),
    "allow_expansion": ("Allow growing PVCs by editing spec.resources.requests.storage (the driver must support "
                        "resizing).", "allowVolumeExpansion", {}),
    "mount_options": ("Mount options for volumes of this class, one per line or comma-separated. Example: "
                      "nfsvers=4.1", "mountOptions[]", {}),
    "labels": ("Labels on the StorageClass, one key=value per line. Example: tier=fast", "metadata.labels", {}),
    "snap_create": ("Also write a VolumeSnapshotClass for this driver so VolumeSnapshot objects can be "
                    "created (needs the snapshot CRDs + controller).", "volumesnapshotclass.yaml", {}),
    "snap_name": ("Name of the VolumeSnapshotClass; VolumeSnapshots reference it in volumeSnapshotClassName. "
                  "Example: csi-snapclass", "VolumeSnapshotClass metadata.name", {}),
    "snap_deletion": ("What happens to the backing snapshot when the VolumeSnapshot is deleted.",
                      "VolumeSnapshotClass deletionPolicy",
                      {"Delete": "delete the storage snapshot too", "Retain": "keep it (VolumeSnapshotContent stays)"}),
    "snap_default": ("Mark it as the default VolumeSnapshotClass for this driver.",
                     "metadata.annotations: snapshot.storage.kubernetes.io/is-default-class", {}),
    "snap_install_crds": ("install.sh also installs the snapshot CRDs and snapshot-controller from "
                          "external-snapshotter (skip on platforms that ship them, e.g. GKE / AKS / EKS add-on).",
                          "install.sh: external-snapshotter client/config/crd + snapshot-controller", {}),
    "install_driver": ("install.sh also installs / upgrades the CSI driver itself (Helm chart or upstream "
                       "manifest, pinned version) before applying the StorageClass.", "install.sh", {}),
    "drv_namespace": ("Namespace the driver is installed into. Example: kube-system", "helm --namespace", {}),
    "drv_version": (f"Driver chart / release version. {VERSION}", "helm --version (or manifest URL)", {}),
    "allowed_topologies": ("Restrict where volumes of this class can be provisioned, e.g. only some zones. "
                           "Empty = anywhere the driver can.", "allowedTopologies[].matchLabelExpressions[]", {}),
    "allowed_topologies.key": ("Topology label key. Example: topology.kubernetes.io/zone",
                               "matchLabelExpressions[].key", {}),
    "allowed_topologies.values": ("Allowed values, comma-separated. Example: us-east-1a, us-east-1b",
                                  "matchLabelExpressions[].values", {}),
    "extra_params": ("Any further driver parameters, one key=value per line; they are added to (and override) "
                     "the form's parameters. Example: csi.storage.k8s.io/fstype=xfs", "parameters", {}),
}

CSI_FIELDS = {
    # AWS EBS
    "ebs_type": ("EBS volume type.", "", {"gp3": "general purpose SSD, baseline 3000 IOPS / 125 MiB/s (default)",
                 "gp2": "older SSD, IOPS scale with size", "io1": "provisioned IOPS SSD", "io2": "provisioned IOPS, higher durability",
                 "st1": "throughput HDD (>= 125 GiB)", "sc1": "cold HDD (>= 125 GiB)", "standard": "magnetic (legacy)"}),
    "ebs_iops": ("Provisioned IOPS for io1 / io2 / gp3 (gp3: 3000-80000). Example: 6000", "", {}),
    "ebs_iops_per_gb": ("IOPS per GiB for io1 / io2 / gp3; the driver multiplies by the volume size. Example: 50", "", {}),
    "ebs_throughput": ("Throughput in MiB/s, gp3 only (125-2000). Example: 250", "", {}),
    "ebs_encrypted": ("Encrypt volumes at rest (AWS-managed key unless kmsKeyId is set).", "", {}),
    "ebs_kms": ("Full ARN of a customer KMS key for encryption; blank = aws/ebs. Example: "
                "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab", "", {}),
    "ebs_fstype": ("Filesystem created on the volume.", "", FS),
    "ebs_auto_iops": ("Raise IOPS to the type's minimum when iopsPerGB x size would be too low, instead of "
                      "failing.", "", {}),
    "ebs_role_arn": ("IAM role (IRSA) for the controller ServiceAccount; blank = node role / Pod Identity. "
                     "Example: arn:aws:iam::111122223333:role/ebs-csi",
                     "values.yaml: controller.serviceAccount.annotations eks.amazonaws.com/role-arn", {}),
    # AWS EFS
    "efs_mode": ("How volumes are provisioned.", "", {"efs-ap": "one EFS access point per PV (dynamic provisioning)"}),
    "efs_fs_id": ("Existing EFS file system the access points are created in. Example: fs-0123456789abcdef0", "", {}),
    "efs_perms": ("Permissions of each access point's root directory (octal). Example: 700", "", {}),
    "efs_uid": ("POSIX user id for the access point; blank = taken from the gid range. Example: 1000", "", {}),
    "efs_gid": ("POSIX group id for the access point; blank = taken from the gid range. Example: 1000", "", {}),
    "efs_gid_start": ("First gid the driver may assign to access points; blank = 50000. Example: 50000", "", {}),
    "efs_gid_end": ("Last gid the driver may assign; blank = 7000000. Example: 7000000", "", {}),
    "efs_base_path": ("Directory under which access-point roots are created. Example: /dynamic_provisioning", "", {}),
    "efs_sub_path": ("Template for each volume's directory under basePath. Example: ${.PVC.namespace}/${.PVC.name}",
                     "", {}),
    "efs_unique": ("Append a UUID to subPathPattern so two PVCs never share a directory; untick to emit "
                   "ensureUniqueDirectory: false.", "", {}),
    "efs_reuse_ap": ("Reuse an existing access point with the same name (static-like re-binding) instead of "
                     "creating a new one.", "", {}),
    "efs_az": ("Availability zone of a One Zone EFS file system; blank for Regional. Example: us-east-1a", "", {}),
    "efs_role_arn": ("IAM role (IRSA) for the EFS controller ServiceAccount. Example: "
                     "arn:aws:iam::111122223333:role/efs-csi",
                     "values.yaml: controller.serviceAccount.annotations eks.amazonaws.com/role-arn", {}),
    # GCE PD
    "gce_type": ("Persistent disk type.", "", {"pd-balanced": "SSD-backed, balanced price / performance (default)",
                 "pd-standard": "HDD", "pd-ssd": "fast SSD", "pd-extreme": "provisioned IOPS (set iops)",
                 "hyperdisk-balanced": "Hyperdisk, tunable IOPS + throughput",
                 "hyperdisk-throughput": "Hyperdisk for streaming throughput",
                 "hyperdisk-extreme": "Hyperdisk, highest IOPS"}),
    "gce_replication": ("Zonal or regional disks.", "", {"none": "zonal disk (default)",
                        "regional-pd": "synchronously replicated across two zones"}),
    "gce_iops": ("IOPS to provision (pd-extreme / hyperdisk). Example: 10000", "", {}),
    "gce_throughput": ("Throughput in MiB/s to provision (hyperdisk). Example: 200", "", {}),
    "gce_kms": ("Cloud KMS key for customer-managed encryption. Example: "
                "projects/p/locations/us-central1/keyRings/r/cryptoKeys/k", "", {}),
    "gce_labels": ("GCP labels added to each disk, key=value comma-separated. Example: team=data,env=prod", "", {}),
    "gce_fstype": ("Filesystem created on the disk.", "", FS),
    # Azure Disk
    "azd_sku": ("Managed disk SKU.", "", {"Premium_ZRS": "premium SSD, zone-redundant (default)",
                "StandardSSD_ZRS": "standard SSD, zone-redundant", "Premium_LRS": "premium SSD, locally redundant",
                "StandardSSD_LRS": "standard SSD", "Standard_LRS": "HDD",
                "PremiumV2_LRS": "Premium SSD v2: tunable IOPS / MBps, cachingMode None",
                "UltraSSD_LRS": "Ultra disk: highest IOPS, cachingMode None"}),
    "azd_caching": ("Host caching of the disk.", "", {"": "driver default (ReadOnly; None for v2 / Ultra)",
                    "None": "no host cache (required for PremiumV2 / Ultra)", "ReadOnly": "cache reads"}),
    "azd_fstype": ("Filesystem created on the disk.", "", FS),
    "azd_iops": ("IOPS for UltraSSD_LRS / PremiumV2_LRS. Example: 5000", "", {}),
    "azd_mbps": ("Throughput (MB/s) for UltraSSD_LRS / PremiumV2_LRS. Example: 200", "", {}),
    "azd_network": ("Network access policy for disk export / import.", "",
                    {"": "Azure default (AllowAll)", "AllowAll": "export from any network",
                     "DenyAll": "no export / import", "AllowPrivate": "only via a disk access private endpoint"}),
    "azd_des": ("Disk encryption set (customer-managed keys) resource ID. Example: "
                "/subscriptions/<id>/resourceGroups/rg/providers/Microsoft.Compute/diskEncryptionSets/des", "", {}),
    "azd_rg": ("Resource group for new disks; blank = the cluster's node resource group. Example: my-disks-rg", "", {}),
    "azd_tags": ("Azure tags on each disk, key=value comma-separated. Example: team=data", "", {}),
    "azd_bursting": ("Enable on-demand bursting (Premium SSD > 512 GiB) for extra IOPS / throughput.", "", {}),
    # Azure File
    "azf_sku": ("Storage account SKU for the file shares.", "", {"Standard_LRS": "standard, locally redundant (default)",
                "Standard_ZRS": "standard, zone-redundant", "Standard_GRS": "standard, geo-redundant",
                "Standard_RAGRS": "geo-redundant with read access", "Premium_LRS": "premium SSD (needed for NFS)",
                "Premium_ZRS": "premium SSD, zone-redundant"}),
    "azf_protocol": ("File share protocol.", "", {"smb": "SMB 3 (default; Windows + Linux)",
                     "nfs": "NFS 4.1 (Linux only, Premium SKU)"}),
    "azf_share": ("Use this existing share name (all PVs share it); blank = a new share per volume. Example: data",
                  "", {}),
    "azf_account": ("Existing storage account to use; blank = the driver finds or creates a matching one. "
                    "Example: mystorageacct", "", {}),
    "azf_rg": ("Resource group of the storage account; blank = the node resource group. Example: storage-rg", "", {}),
    "azf_endpoint": ("Restrict the storage account to the cluster's VNet.", "",
                     {"": "public endpoint (default)", "privateEndpoint": "create a private endpoint in the VNet",
                      "serviceEndpoint": "VNet service endpoint"}),
    "azf_large": ("Enable large file shares (up to 100 TiB) on new standard storage accounts.", "", {}),
    # vSphere
    "vs_policy": ("vCenter storage policy (SPBM) that selects compatible datastores. Example: vSAN Default "
                  "Storage Policy", "", {}),
    "vs_datastore": ("Pin volumes to one datastore by URL; blank = any compatible one. Example: "
                     "ds:///vmfs/volumes/vsan:52c.../", "", {}),
    "vs_fstype": ("Filesystem created on the volume.", "", FS),
    # Nutanix
    "nx_storage_type": ("Nutanix storage backend.", "", {"NutanixVolumes": "block volumes over iSCSI (RWO)",
                        "NutanixFiles": "NFS shares on Nutanix Files (RWX)"}),
    "nx_container": ("Prism storage container for NutanixVolumes. Example: default-container", "", {}),
    "nx_fstype": ("Filesystem on NutanixVolumes block volumes.", "", FS),
    "nx_segmented": ("Tick when iSCSI uses a separate (segmented) data-services network.", "", {}),
    "nx_flash": ("Pin volumes to the SSD tier (flashMode ENABLED).", "", {}),
    "nx_chap": ("Use CHAP authentication for iSCSI (chapAuth ENABLED).", "", {}),
    "nx_description": ("Description stored on each Nutanix volume group / share. Example: k8s prod volumes", "", {}),
    "nx_nfs_server": ("NutanixFiles with an existing share: the Files server address. Example: files.example.com",
                      "", {}),
    "nx_nfs_path": ("NutanixFiles with an existing share: the export path. Example: /k8s-share", "", {}),
    "nx_files_server": ("Dynamic NutanixFiles shares: the Files server name in Prism. Example: files01", "", {}),
    "nx_squash": ("Root squashing on dynamically created shares.", "", {"root-squash": "map root to nobody (default)",
                  "none": "no squashing", "all-squash": "map every user to nobody"}),
    "nx_create_secret": ("Write secret.yaml (mode 0600) with the Prism credentials in the driver's key format "
                         "(base64 of endpoint:port:user:password).", "secret.yaml: data.key", {}),
    "nx_secret_name": ("Secret the StorageClass references for provisioning / publishing. Example: ntnx-secret",
                       "parameters csi.storage.k8s.io/*-secret-name", {}),
    "nx_secret_namespace": ("Namespace of that Secret. Example: ntnx-system",
                            "parameters csi.storage.k8s.io/*-secret-namespace", {}),
    "nx_prism": ("Prism Element address (cluster virtual IP or FQDN). Example: 10.0.0.50", "secret.yaml: key (field 1)", {}),
    "nx_prism_port": ("Prism Element API port (part of the Secret key). Example: 9440", "secret.yaml: key (field 2)", {}),
    "nx_user": ("Prism user the driver authenticates as. Example: k8s-csi", "secret.yaml: key (field 3)", {}),
    "nx_password": ("Password of that user; only written into secret.yaml (0600).", "secret.yaml: key (field 4)", {}),
    "nx_dynamic": ("NutanixFiles: create a new share per volume on the Files server (dynamicProv ENABLED) "
                   "instead of sub-directories of an existing share.", "parameters: dynamicProv", {}),
    # NFS
    "nfs_server": ("NFS server address (DNS name or IP). Example: nfs.example.com", "", {}),
    "nfs_share": ("Exported path on the server; volumes become sub-directories. Example: /exports", "", {}),
    "nfs_subdir": ("Template for each volume's sub-directory; blank = one directory per PV. Example: "
                   "${pvc.metadata.namespace}/${pvc.metadata.name}", "", {}),
    "nfs_perms": ("chmod applied to new volume directories (octal); blank = no chmod. Example: 0777", "", {}),
    "nfs_on_delete": ("What happens to the directory when the PV is deleted.", "", ONDEL),
    "nfs_demo_server": ("Also write nfs-server.yaml: an in-cluster NFS-Ganesha server (userspace, emptyDir) "
                        "for testing; server/share are set to it.", "nfs-server.yaml", {}),
    "nfs_demo_namespace": ("Namespace of the demo NFS server. Example: nfs-demo", "nfs-server.yaml: metadata.namespace", {}),
    # SMB
    "smb_source": ("SMB share the volumes live in. Example: //smb.example.com/share", "", {}),
    "smb_subdir": ("Sub-directory template inside the share; blank = one directory per PV. Example: "
                   "${pvc.metadata.name}", "", {}),
    "smb_on_delete": ("What happens to the directory when the PV is deleted.", "", ONDEL),
    "smb_create_secret": ("Write secret.yaml (mode 0600) with the SMB username / password (base64).",
                          "secret.yaml", {}),
    "smb_secret_name": ("Secret with the SMB credentials. Example: smbcreds",
                        "parameters csi.storage.k8s.io/*-secret-name", {}),
    "smb_secret_namespace": ("Namespace of that Secret. Example: default",
                             "parameters csi.storage.k8s.io/*-secret-namespace", {}),
    "smb_user": ("SMB user name the driver mounts the share with. Example: k8s", "secret.yaml: data.username", {}),
    "smb_password": ("SMB password; only written into secret.yaml (0600).", "secret.yaml: data.password", {}),
    "smb_domain": ("Windows / AD domain of the user; blank = none. Example: CORP", "secret.yaml: data.domain", {}),
    # Longhorn
    "lh_replicas": ("Copies of each volume on different nodes. Example: 3 (1 on single-node clusters)", "", {}),
    "lh_stale": ("Minutes before a failed replica is cleaned up. Example: 2880", "", {}),
    "lh_locality": ("Keep a replica on the node that runs the pod.", "", {"disabled": "no preference (default)",
                    "best-effort": "try to keep one replica local", "strict-local": "single local replica (needs 1 replica)"}),
    "lh_fstype": ("Filesystem created on the volume.", "", FS),
    "lh_engine": ("Longhorn data engine.", "", {"v1": "iSCSI-based engine (stable)", "v2": "SPDK engine (needs huge pages)"}),
    "lh_disk_selector": ("Only use disks with these Longhorn tags, comma-separated. Example: ssd", "", {}),
    "lh_node_selector": ("Only use nodes with these Longhorn tags, comma-separated. Example: storage", "", {}),
    "lh_snap_type": ("What a VolumeSnapshot creates.", "VolumeSnapshotClass parameters.type",
                     {"snap": "in-cluster Longhorn snapshot", "bak": "backup to the configured backup target"}),
    # Rook
    "rk_cluster_id": ("Namespace of the CephCluster (Rook's clusterID); the CSI secrets live there. "
                      "Example: rook-ceph", "", {}),
    "rk_pool": ("CephBlockPool the RBD images are created in. Example: replicapool", "", {}),
    "rk_image_format": ("RBD image format.", "", {"2": "format 2 (the only supported format)"}),
    "rk_features": ("RBD image features, comma-separated. Example: layering,fast-diff,object-map,deep-flatten,"
                    "exclusive-lock", "", {}),
    "rk_fstype": ("Filesystem created on the RBD image.", "", FS),
    "rk_fs_name": ("CephFilesystem the volumes are created in. Example: myfs", "", {}),
    "rk_fs_pool": ("Data pool of the filesystem (<fsName>-<pool name> for Rook-created pools). Example: "
                   "myfs-replicated", "", {}),
    "rk_replicas": ("Replica count of the generated pool(s). Example: 3 (1 for a test cluster)",
                    "ceph-pool.yaml: replicated.size", {}),
    "rk_failure_domain": ("Where replicas are spread.", "ceph-pool.yaml: spec.failureDomain",
                          {"host": "one replica per node (default)", "osd": "per OSD (single-node tests)",
                           "zone": "per zone", "rack": "per rack (needs CRUSH topology labels)"}),
    # OpenEBS
    "oe_base_path": ("Directory on each node where hostpath volumes are created. Example: /var/openebs/local",
                     "metadata.annotations cas.openebs.io/config: BasePath", {}),
    # TopoLVM
    "tv_device_class": ("lvmd device class the volumes come from. Example: ssd", "", {}),
    "tv_fstype": ("Filesystem created on the logical volume.", "", FS),
    "tv_vg": ("LVM volume group lvmd uses for that device class (must exist on the nodes). Example: myvg1",
              "values.yaml: lvmd.deviceClasses[].volume-group", {}),
    "tv_spare": ("GiB kept free in the volume group (not offered for volumes). Example: 10",
                 "values.yaml: lvmd.deviceClasses[].spare-gb", {}),
    "tv_cert_manager": ("Install cert-manager as a chart dependency (TopoLVM's webhook needs it).",
                        "values.yaml: cert-manager.enabled", {}),
    # local-path
    "lp_node_path": ("Use only this node path from the provisioner's nodePathMap; blank = any. Example: "
                     "/opt/local-path-provisioner", "", {}),
    "lp_path_pattern": ("Directory layout under the node path (Go template). Example: "
                        "{{ .PVC.Namespace }}/{{ .PVC.Name }}/data", "", {}),
    "lp_volume_type": ("Kind of PV the provisioner creates.", "metadata.annotations: defaultVolumeType",
                       {"": "provisioner default (hostPath)", "hostPath": "hostPath PV", "local": "local PV with node affinity"}),
}

TYPE_OVERRIDES = {
    ("Rook-Ceph RBD", "rk_create_pool"): ("Also write ceph-pool.yaml with a CephBlockPool of that name (needs a "
                                          "running CephCluster).", "ceph-pool.yaml: CephBlockPool", {}),
    ("Rook-Ceph CephFS", "rk_create_pool"): ("Also write ceph-pool.yaml with a CephFilesystem (fsName + data "
                                             "pool, MDS 1 active + standby).", "ceph-pool.yaml: CephFilesystem", {}),
    ("Rook-Ceph RBD", "drv_namespace"): ("Namespace of the Rook operator; it prefixes the provisioner name "
                                         "(<ns>.rbd.csi.ceph.com). Example: rook-ceph", "helm --namespace", {}),
    ("Rook-Ceph CephFS", "drv_namespace"): ("Namespace of the Rook operator; it prefixes the provisioner name "
                                            "(<ns>.cephfs.csi.ceph.com). Example: rook-ceph", "helm --namespace", {}),
}

SECTIONS = {
    "Version & install": "Which release install.sh installs and how it waits; versions are pinned and verified.",
    "IP pools": "Pod address ranges (Calico IPPools). The CIDR should match the cluster's pod subnet.",
    "Networking": "Routing, MTU, dataplane and node address detection for Calico.",
    "eBPF dataplane": "Settings for linuxDataplane BPF: kube-proxy handling and the real API server address.",
    "Components": "Optional Calico components: API server, Goldmane flow aggregator, Whisker UI.",
    "IPAM": "Where pod / interface IP addresses come from.",
    "Datapath": "kube-proxy replacement, routing mode, tunnels, masquerading and MTU.",
    "Hubble": "Hubble network observability: agent flows, Relay API and UI.",
    "Security": "Transparent encryption and network-policy enforcement mode.",
    "Features": "Optional Cilium features: BGP control plane, Gateway API, L2 announcements, operator size.",
    "net-conf.json": "Flannel's network config: pod CIDR, per-node subnet size, IPv6, nftables.",
    "Backend": "How flannel carries traffic between nodes (VXLAN, host-gw, WireGuard...).",
    "canal-config": "Settings of the canal-config ConfigMap (flannel networking + Calico policy).",
    "antrea-config (agent)": "Antrea agent settings: encapsulation, tunnel, encryption, MTU, interfaces.",
    "Proxy & IPAM": "Antrea Proxy (kube-proxy replacement), NodeIPAM and feature gates.",
    "NetworkAttachmentDefinition": "The secondary network pods can attach to via the networks annotation.",
    "Example pod": "A test pod that requests the secondary network.",
    "StorageClass": "Common StorageClass settings: name, default class, reclaim / binding policy, expansion.",
    "Driver secret": "Credentials Secret the StorageClass references (written to secret.yaml with mode 0600).",
    "VolumeSnapshotClass": "Optional VolumeSnapshotClass for the driver and the snapshot CRDs / controller.",
    "Driver install": "Optional driver installation in install.sh (Helm chart or manifest, pinned version).",
    "Allowed topologies": "Restrict provisioning to some zones / nodes (allowedTopologies).",
    "Extra parameters": "Free-form extra StorageClass parameters (key=value per line).",
}


def section_help(atype: str, title: str) -> str:
    if title in SECTIONS:
        return SECTIONS[title]
    if title.endswith(" parameters"):
        return (f"{title[:-11]} driver parameters written to StorageClass.parameters (names as in the driver's "
                "documentation).")
    return ""
