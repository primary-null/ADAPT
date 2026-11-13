package org.adaptadb.dex;

import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Set;

/** ADAPT's locally built Samsung display capability probe. No app installation. */
public final class AdaptDexBridge {
    private static volatile boolean running = true;
    private static volatile long heartbeat = System.currentTimeMillis();
    private static Object manager;
    private static Class<?> iface;
    private static String token, name, mac;
    private static Socket control;
    private static DatagramSocket rtp, rtcp;
    private static String quote(String value) {
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", " ").replace("\r", " ") + "\"";
    }

    private static Object call(Object object, String method) throws Exception {
        return object.getClass().getMethod(method).invoke(object);
    }
    private static void event(String type, String fields) {
        System.out.println("{\"event\":" + quote(type) + (fields.isEmpty() ? "" : "," + fields) + "}");
        System.out.flush();
    }
    private static void init() throws Exception {
        Class<?> binder = Class.forName("android.os.IBinder");
        Object service = Class.forName("android.os.ServiceManager").getMethod("getService", String.class).invoke(null, "display");
        manager = Class.forName("android.hardware.display.IDisplayManager$Stub").getMethod("asInterface", binder).invoke(null, service);
        iface = Class.forName("android.hardware.display.IDisplayManager");
    }
    private static Object wifi() throws Exception { return iface.getMethod("getWifiDisplayStatus").invoke(manager); }
    private static int wifiState() throws Exception { return (Integer) call(wifi(), "getActiveDisplayState"); }
    private static boolean owned() throws Exception {
        Object active = call(wifi(), "getActiveDisplay");
        return active != null && mac.equalsIgnoreCase(String.valueOf(call(active, "getDeviceAddress")))
                && name.equals(String.valueOf(call(active, "getDeviceName")));
    }
    private static void cleanup() {
        running = false;
        try {
            if (owned()) {
                iface.getMethod("disconnectWifiDisplay").invoke(manager);
                event("cleanup", "\"status\":\"disconnect_requested\"");
            } else event("cleanup", "\"status\":\"not_owner_no_disconnect\"");
        } catch (Exception e) { event("cleanup", "\"status\":\"unverified\""); }
        try { if (control != null) control.close(); } catch (Exception ignored) { }
        if (rtp != null) rtp.close();
        if (rtcp != null) rtcp.close();
    }
    private static int[] displays() throws Exception {
        Class<?> global = Class.forName("android.hardware.display.DisplayManagerGlobal");
        Object instance = global.getMethod("getInstance").invoke(null);
        return (int[]) global.getMethod("getDisplayIds").invoke(instance);
    }
    private static int displayType(int id) throws Exception {
        Class<?> global = Class.forName("android.hardware.display.DisplayManagerGlobal");
        Object instance = global.getMethod("getInstance").invoke(null);
        Object info = global.getMethod("getDisplayInfo", int.class).invoke(instance, id);
        if (info == null) throw new IllegalStateException("Display changed during inspection");
        return info.getClass().getField("type").getInt(info);
    }
    private static String displayUniqueId(int id) throws Exception {
        Class<?> global = Class.forName("android.hardware.display.DisplayManagerGlobal");
        Object instance = global.getMethod("getInstance").invoke(null);
        Object info = global.getMethod("getDisplayInfo", int.class).invoke(instance, id);
        return String.valueOf(info.getClass().getField("uniqueId").get(info));
    }
    private static int[] dexState() throws Exception {
        Class<?> binder = Class.forName("android.os.IBinder");
        Object service = Class.forName("android.os.ServiceManager").getMethod("getService", String.class).invoke(null, "desktopmode");
        Class<?> desktop = Class.forName("com.samsung.android.desktopmode.IDesktopMode");
        Object proxy = Class.forName("com.samsung.android.desktopmode.IDesktopMode$Stub").getMethod("asInterface", binder).invoke(null, service);
        Object state = desktop.getMethod("getDesktopModeState").invoke(proxy);
        int enabled = state.getClass().getField("enabled").getInt(state);
        int type;
        try { type = state.getClass().getField("displayType").getInt(state); }
        catch (NoSuchFieldException ignored) { type = (Integer) call(state, "getDisplayType"); }
        return new int[]{enabled, type};
    }
    private static void probe() throws Exception {
        List<String> methods = new ArrayList<String>();
        boolean config = false;
        try {
            Class.forName("android.hardware.display.SemWifiDisplayConfig$Builder");
            config = true;
        } catch (ClassNotFoundException ignored) { }
        for (Method method : iface.getMethods()) {
            if (method.getName().toLowerCase().contains("wifidisplay")) {
                methods.add(quote(method.toString()));
            }
        }
        int[] dex = dexState();
        List<String> screens = new ArrayList<String>();
        for (int id : displays()) screens.add("{\"id\":" + id + ",\"type\":" + displayType(id) + ",\"unique_id\":" + quote(displayUniqueId(id)) + "}");
        event("probe", "\"config_builder\":" + config + ",\"wifi_state\":" + wifiState()
                + ",\"dex_enabled\":" + dex[0] + ",\"dex_display_type\":" + dex[1]
                + ",\"displays\":[" + String.join(",", screens) + "],\"methods\":[" + String.join(",", methods) + "]");
    }
    private static void drain(final DatagramSocket socket) {
        Thread thread = new Thread(() -> {
            byte[] packet = new byte[65536];
            while (running) {
                try { socket.receive(new DatagramPacket(packet, packet.length)); }
                catch (IOException e) { return; }
            }
        }, "adapt-rtp-drain");
        thread.setDaemon(true); thread.start();
    }
    private static void send(OutputStream out, String line) throws IOException {
        out.write(line.getBytes(StandardCharsets.US_ASCII)); out.flush();
    }
    private static void respond(OutputStream out, String cseq, String body) throws IOException {
        send(out, "RTSP/1.0 200 OK\r\nCSeq: " + cseq + "\r\nContent-Type: text/parameters\r\nContent-Length: "
                + body.getBytes(StandardCharsets.US_ASCII).length + "\r\n\r\n" + body);
    }
    private static String readLine(InputStream input) throws IOException {
        ByteArrayOutputStream line = new ByteArrayOutputStream();
        for (int c; (c = input.read()) != -1;) {
            if (c == '\n') return new String(line.toByteArray(), StandardCharsets.US_ASCII).replace("\r", "");
            if (line.size() >= 8192) throw new IOException("Oversized RTSP header");
            line.write(c);
        }
        return null;
    }
    private static void rtsp() throws Exception {
        InputStream in = control.getInputStream(); OutputStream out = control.getOutputStream();
        int sequence = 1, setupSequence = -1, playSequence = -1;
        String url = "rtsp://127.0.0.1/wfd1.0/streamid=0", session = null;
        while (running) {
            String first = readLine(in);
            if (first == null) return;
            if (first.isEmpty()) continue;
            Map<String,String> headers = new HashMap<String,String>();
            for (int count = 0; ; count++) {
                String line = readLine(in);
                if (line == null || count > 64) throw new IOException("Incomplete RTSP headers");
                if (line.isEmpty()) break;
                int colon = line.indexOf(':');
                if (colon > 0) headers.put(line.substring(0, colon).toLowerCase(java.util.Locale.ROOT), line.substring(colon + 1).trim());
            }
            int length = Integer.parseInt(headers.containsKey("content-length") ? headers.get("content-length") : "0");
            if (length < 0 || length > 65536) throw new IOException("Oversized RTSP body");
            byte[] data = new byte[length];
            for (int pos = 0; pos < length;) {
                int n = in.read(data, pos, length - pos); if (n < 0) throw new EOFException(); pos += n;
            }
            String body = new String(data, StandardCharsets.US_ASCII), cseq = headers.get("cseq");
            event("rtsp", "\"method\":" + quote(first.split(" ")[0]));
            if (first.startsWith("OPTIONS ")) {
                send(out, "RTSP/1.0 200 OK\r\nCSeq: " + cseq + "\r\nPublic: org.wfa.wfd1.0, GET_PARAMETER, SET_PARAMETER\r\n\r\n");
                send(out, "OPTIONS * RTSP/1.0\r\nCSeq: " + sequence++ + "\r\nRequire: org.wfa.wfd1.0\r\n\r\n");
            } else if (first.startsWith("GET_PARAMETER ")) {
                String capabilities = body.contains("wfd_video_formats")
                    ? "wfd_client_rtp_ports: RTP/AVP/UDP;unicast " + rtp.getLocalPort() + " 0 mode=play\r\n"
                      + "wfd_audio_codecs: LPCM 00000003 00\r\n"
                      + "wfd_video_formats: 00 00 02 02 00000020 00000000 00000000 00 0000 0000 00 none none\r\n"
                      + "wfd_content_protection: none\r\nwfd_uibc_capability: none\r\n" : "";
                respond(out, cseq, capabilities);
            } else if (first.startsWith("SET_PARAMETER ")) {
                for (String line : body.split("\r?\n")) if (line.startsWith("wfd_presentation_URL:")) {
                    String proposed = line.substring("wfd_presentation_URL:".length()).trim().split("\\s+")[0];
                    URI parsed = new URI(proposed);
                    if (!"rtsp".equals(parsed.getScheme()) || !"127.0.0.1".equals(parsed.getHost()) || parsed.getUserInfo() != null) throw new IOException("Non-loopback presentation URL");
                    url = proposed;
                }
                respond(out, cseq, "");
                if (body.contains("wfd_trigger_method: SETUP")) {
                    setupSequence = sequence++;
                    send(out, "SETUP " + url + " RTSP/1.0\r\nCSeq: " + setupSequence + "\r\nTransport: RTP/AVP/UDP;unicast;client_port=" + rtp.getLocalPort() + "-" + rtcp.getLocalPort() + "\r\n\r\n");
                } else if (body.contains("wfd_trigger_method: TEARDOWN")) return;
            } else if (first.startsWith("RTSP/1.0 200 ")) {
                int reply = Integer.parseInt(cseq);
                if (reply == setupSequence && headers.containsKey("session")) {
                    session = headers.get("session").split(";")[0]; playSequence = sequence++;
                    send(out, "PLAY " + url + " RTSP/1.0\r\nCSeq: " + playSequence + "\r\nSession: " + session + "\r\n\r\n");
                } else if (reply == playSequence) event("playing", "");
            } else throw new IOException("Unsupported RTSP response");
        }
    }
    private static void serve() throws Exception {
        if (wifiState() != 0) throw new IllegalStateException("Existing wireless display session; refusing replacement");
        if (dexState()[0] == 4) throw new IllegalStateException("Existing DeX session; refusing replacement");
        Set<Integer> baseline = new HashSet<Integer>();
        for (int display : displays()) {
            if (displayType(display) != 1) throw new IllegalStateException("Existing non-internal display; refusing replacement");
            baseline.add(display);
        }
        try (ServerSocket check = new ServerSocket()) { check.bind(new InetSocketAddress("127.0.0.1", 7236)); }
        java.security.SecureRandom random = new java.security.SecureRandom();
        for (int attempt = 0; attempt < 32 && rtcp == null; attempt++) {
            int port = 30000 + 2 * random.nextInt(15000);
            try {
                rtp = new DatagramSocket(new InetSocketAddress("127.0.0.1", port));
                rtcp = new DatagramSocket(new InetSocketAddress("127.0.0.1", port + 1));
            } catch (IOException e) { if (rtp != null) rtp.close(); rtp = null; }
        }
        if (rtp == null || rtcp == null) throw new IOException("No local RTP port pair available");
        drain(rtp); drain(rtcp);
        Class<?> builderClass = Class.forName("android.hardware.display.SemWifiDisplayConfig$Builder");
        Object builder = builderClass.getConstructor().newInstance();
        builderClass.getMethod("setApConnection", String.class, String.class, String.class, String.class).invoke(builder, "127.0.0.1", "7236", name, mac);
        builderClass.getMethod("setMode", int.class).invoke(builder, 2);
        Object config = builderClass.getMethod("build").invoke(builder);
        Method connect = iface.getMethod("connectWifiDisplayWithConfig", Class.forName("android.hardware.display.SemWifiDisplayConfig"), Class.forName("android.hardware.display.IWifiDisplayConnectionCallback"));
        Class<?> callback = connect.getParameterTypes()[1];
        Object binder = Class.forName("android.os.Binder").getConstructor().newInstance();
        Object listener = Proxy.newProxyInstance(AdaptDexBridge.class.getClassLoader(), new Class<?>[]{callback}, (p,m,a) -> {
            if (m.getName().equals("asBinder")) return binder;
            if (m.getName().equals("hashCode")) return 0;
            if (m.getName().equals("equals")) return p == a[0];
            if (m.getName().equals("toString")) return "ADAPT callback";
            event("callback", "\"method\":" + quote(m.getName())); return null;
        });
        Runtime.getRuntime().addShutdownHook(new Thread(() -> cleanup()));
        connect.invoke(manager, config, listener);
        event("connecting", "");
        long deadline = System.currentTimeMillis() + 20000;
        while (control == null && System.currentTimeMillis() < deadline) {
            Socket s = new Socket();
            try { s.connect(new InetSocketAddress("127.0.0.1", 7236), 300); control = s; }
            catch (IOException e) { s.close(); Thread.sleep(150); }
        }
        if (control == null) throw new IOException("Loopback listener did not become available");
        control.setSoTimeout(65000);
        Thread protocol = new Thread(() -> {
            try { rtsp(); } catch (Exception e) { event("error", "\"stage\":\"rtsp\",\"kind\":" + quote(e.getClass().getSimpleName())); }
            running = false;
        }, "adapt-rtsp"); protocol.setDaemon(true); protocol.start();
        Thread input = new Thread(() -> {
            try { BufferedReader reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.US_ASCII));
                for (String line; (line = reader.readLine()) != null;) {
                    if (line.equals("STOP")) break; if (line.equals("PING")) heartbeat = System.currentTimeMillis();
                }
            } catch (IOException ignored) { } running = false;
        }, "adapt-heartbeat"); input.setDaemon(true); input.start();
        deadline = System.currentTimeMillis() + 40000; boolean ready = false;
        while (running && System.currentTimeMillis() - heartbeat < 20000) {
            if (!ready) {
                int[] state = dexState(), ids = displays(); int candidate = -1;
                for (int id : ids) if (!baseline.contains(id)) { if (candidate != -1) throw new IllegalStateException("Multiple new displays"); candidate = id; }
                if (candidate != -1 && displayType(candidate) == 3 && state[0] == 4 && state[1] == 102 && owned()) {
                    ready = true; event("ready", "\"display_id\":" + candidate + ",\"enabled\":4,\"display_type\":102");
                }
                if (!ready && System.currentTimeMillis() > deadline) throw new IOException("Samsung DeX activation timed out");
            } else if (!owned()) break;
            Thread.sleep(250);
        }
        cleanup();
    }
    public static void main(String[] args) {
        try {
            init();
            if (args.length == 1 && "probe".equals(args[0])) { probe(); return; }
            if (args.length != 2 || !"serve".equals(args[0]) || !args[1].matches("[0-9a-f]{32}")) throw new IllegalArgumentException("Expected probe or serve SESSION_TOKEN");
            token = args[1]; name = "ADAPT DeX " + token.substring(0, 8);
            mac = "02:ad:" + token.substring(0,2) + ":" + token.substring(2,4) + ":" + token.substring(4,6) + ":" + token.substring(6,8);
            serve();
        } catch (Throwable e) {
            Throwable cause = e.getCause() == null ? e : e.getCause();
            event("error", "\"stage\":\"activation\",\"kind\":" + quote(cause.getClass().getSimpleName()) + ",\"message\":" + quote(String.valueOf(cause.getMessage())));
            if (token != null) cleanup();
            System.exit(1);
        }
    }
}
