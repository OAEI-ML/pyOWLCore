package org.pyowlcore.comparator;

import com.sun.jna.Library;
import com.sun.jna.Native;
import com.sun.jna.NativeLong;
import com.sun.jna.Structure;

/** Minimal getrusage binding used only by the isolated comparator process. */
final class NativeUsage {
    private interface LibC extends Library {
        LibC INSTANCE = Native.load("c", LibC.class);
        int getrusage(int who, RUsage usage);
    }

    @Structure.FieldOrder({"tvSec", "tvUsec"})
    public static final class Timeval extends Structure {
        public NativeLong tvSec;
        public NativeLong tvUsec;
    }

    @Structure.FieldOrder({
        "user", "system", "maxRss", "integralShared", "integralUnsharedData",
        "integralUnsharedStack", "pageReclaims", "pageFaults", "swaps", "blockInputs",
        "blockOutputs", "messagesSent", "messagesReceived", "signals", "voluntarySwitches",
        "involuntarySwitches"
    })
    public static final class RUsage extends Structure {
        public Timeval user = new Timeval();
        public Timeval system = new Timeval();
        public NativeLong maxRss;
        public NativeLong integralShared;
        public NativeLong integralUnsharedData;
        public NativeLong integralUnsharedStack;
        public NativeLong pageReclaims;
        public NativeLong pageFaults;
        public NativeLong swaps;
        public NativeLong blockInputs;
        public NativeLong blockOutputs;
        public NativeLong messagesSent;
        public NativeLong messagesReceived;
        public NativeLong signals;
        public NativeLong voluntarySwitches;
        public NativeLong involuntarySwitches;
    }

    static final class Snapshot {
        final long cpuNs;
        final long peakRssBytes;

        Snapshot(long cpuNs, long peakRssBytes) {
            this.cpuNs = cpuNs;
            this.peakRssBytes = peakRssBytes;
        }
    }

    private NativeUsage() {}

    static Snapshot snapshot() {
        RUsage usage = new RUsage();
        if (LibC.INSTANCE.getrusage(0, usage) != 0) {
            throw new IllegalStateException("getrusage failed");
        }
        usage.read();
        long cpu = Math.addExact(timeNs(usage.user), timeNs(usage.system));
        long rss = usage.maxRss.longValue();
        if (rss < 0) {
            throw new IllegalStateException("getrusage reported negative peak RSS");
        }
        String operatingSystem = System.getProperty("os.name", "").toLowerCase(java.util.Locale.ROOT);
        if (!operatingSystem.contains("mac")) {
            rss = Math.multiplyExact(rss, 1024L);
        }
        return new Snapshot(cpu, rss);
    }

    private static long timeNs(Timeval value) {
        long seconds = value.tvSec.longValue();
        long micros = value.tvUsec.longValue();
        if (seconds < 0 || micros < 0) {
            throw new IllegalStateException("getrusage reported negative CPU time");
        }
        return Math.addExact(Math.multiplyExact(seconds, 1_000_000_000L),
                Math.multiplyExact(micros, 1_000L));
    }
}
