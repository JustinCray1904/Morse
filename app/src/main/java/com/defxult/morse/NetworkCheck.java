package com.defxult.morse;

import android.content.Context;
import android.content.Intent;
import android.provider.Settings;
import android.util.Log;

import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;

/**
 * Determines whether a usable (non-loopback) IP is available for the server
 * to bind to, and provides the best available intent to the system hotspot
 * settings screen.
 *
 * The IP finder walks the network interfaces directly instead of relying on
 * the "connect to 8.8.8.8" trick. That trick only works when the phone has a
 * route to the internet — which is false when the phone itself is the hotspot.
 * Interface enumeration sees both WiFi and hotspot addresses regardless.
 */
public final class NetworkCheck {

    private static final String TAG = "morse";

    public static final class Result {
        public final boolean ok;
        public final String ip;

        Result(boolean ok, String ip) {
            this.ok = ok;
            this.ip = ip;
        }
    }

    private NetworkCheck() {}

    /**
     * Returns the best local IPv4 to serve on, or null if none is available.
     * Prefers hotspot interfaces, then WiFi station, then USB/eth tethering.
     * Cellular (rmnet) is deliberately excluded — phones on carrier NAT are
     * not reachable from a PC on the LAN.
     */
    public static String findUsableIp() {
        try {
            Enumeration<NetworkInterface> ifaces = NetworkInterface.getNetworkInterfaces();
            List<RankedIp> ranked = new ArrayList<>();
            while (ifaces.hasMoreElements()) {
                NetworkInterface iface = ifaces.nextElement();
                try {
                    if (!iface.isUp() || iface.isLoopback()) continue;
                } catch (Exception e) {
                    continue;
                }
                String name = iface.getName() == null ? "" : iface.getName().toLowerCase();
                int rank = interfaceRank(name);
                if (rank < 0) continue;
                Enumeration<InetAddress> addrs = iface.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress addr = addrs.nextElement();
                    if (addr.isLoopbackAddress()) continue;
                    if (!(addr instanceof Inet4Address)) continue;
                    String ip = addr.getHostAddress();
                    if (ip == null || ip.isEmpty() || "0.0.0.0".equals(ip)) continue;
                    ranked.add(new RankedIp(rank, ip));
                }
            }
            if (ranked.isEmpty()) return null;
            Collections.sort(ranked, (a, b) -> Integer.compare(b.rank, a.rank));
            return ranked.get(0).ip;
        } catch (Exception e) {
            Log.w(TAG, "interface enumeration failed", e);
            return null;
        }
    }

    /**
     * Returns a priority score for an interface name. Higher is better.
     * Returns -1 to exclude the interface entirely (cellular, virtual, etc).
     */
    private static int interfaceRank(String name) {
        if (name.startsWith("ap") || name.contains("softap") || name.startsWith("swlan")) {
            return 100; // hotspot
        }
        if (name.startsWith("wlan") && !name.startsWith("wlan1")) {
            return 80;  // WiFi station (wlan0)
        }
        if (name.startsWith("rndis") || name.startsWith("eth") || name.startsWith("usb")) {
            return 60;  // USB / ethernet tethering
        }
        if (name.startsWith("wlan")) {
            return 50;  // secondary wlan (some devices expose hotspot as wlan1)
        }
        if (name.startsWith("rmnet") || name.startsWith("ccmni") || name.startsWith("pdp")) {
            return -1;  // cellular — excluded, unreachable from PC
        }
        if (name.startsWith("p2p") || name.startsWith("dummy") || name.startsWith("sit")
                || name.startsWith("ip6") || name.startsWith("tun") || name.startsWith("ppp")) {
            return -1;  // virtual / WiFi Direct / VPN — excluded
        }
        return 10; // anything else with an IPv4, last resort
    }

    private static final class RankedIp {
        final int rank;
        final String ip;
        RankedIp(int rank, String ip) { this.rank = rank; this.ip = ip; }
    }

    public static Result check() {
        String ip = findUsableIp();
        if (ip == null) return new Result(false, null);
        return new Result(true, ip);
    }

    /**
     * Best-effort intent to the system hotspot / tethering settings screen.
     */
    public static Intent hotspotSettingsIntent(Context ctx) {
        Intent[] candidates = new Intent[] {
            new Intent().setAction("android.settings.TETHER_SETTINGS"),
            new Intent(Settings.ACTION_WIRELESS_SETTINGS),
            new Intent(Settings.ACTION_WIFI_SETTINGS),
        };
        for (Intent i : candidates) {
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            if (i.resolveActivity(ctx.getPackageManager()) != null) {
                return i;
            }
        }
        Intent i = new Intent(Settings.ACTION_SETTINGS);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return i;
    }
}
