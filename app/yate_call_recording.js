// Loaded by Yate's javascript module when Cliniva SIP call recording is enabled.
// Yate's chan.record message records both audio directions independently.
var clinivaRecordingsDir = __RECORDINGS_DIR__;
var clinivaRecordedCallPairs = {};

Engine.debug(Engine.DebugInfo, "Cliniva: script d'enregistrement des appels chargé");

function clinivaSafeRecordingPart(value) {
    var source = String(value || "unknown");
    var safe = "";
    var index;
    var code;
    for (index = 0; index < source.length && safe.length < 96; index++) {
        code = source.charCodeAt(index);
        if ((code >= 48 && code <= 57) || (code >= 65 && code <= 90) ||
                (code >= 97 && code <= 122) || code === 45 || code === 46)
            safe += source.charAt(index);
        else
            safe += "-";
    }
    return safe || "unknown";
}

function clinivaRecordAnsweredCall(msg) {
    var localId = String(msg.id || "");
    var peerId = String(msg.targetid || "");
    if (!localId) {
        Engine.alarm(Engine.DebugWarn, "Cliniva: appel répondu sans identifiant de canal; enregistrement ignoré");
        return;
    }

    // Some Yate builds omit targetid on call.answered. The recording message
    // only requires the answered channel id; keep a stable fallback identity.
    var pairKey = localId + "__" + (peerId.length ? peerId : "unknown");
    var billid = clinivaSafeRecordingPart(msg.billid);
    var dedupeKey = pairKey;
    if (billid !== "unknown")
        dedupeKey = "billid__" + billid;
    if (clinivaRecordedCallPairs[dedupeKey])
        return;

    var stamp = String(new Date().getTime());
    var base = "billid__" + billid + "__" + pairKey + "__" +
        clinivaSafeRecordingPart(msg.caller) + "__" +
        clinivaSafeRecordingPart(msg.called) + "__" + stamp;
    var folder = clinivaRecordingsDir;
    if (folder.charAt(folder.length - 1) === "/")
        folder = folder.substring(0, folder.length - 1);
    var waveRecordPrefix = "wave/record/";
    var record = new Message("chan.masquerade");
    record.message = "chan.record";
    record.id = localId;
    // Yate's wavefile resource needs a double slash before an absolute path.
    // Without it, Windows paths such as C:/... are parsed as relative resources.
    record.call = waveRecordPrefix + "/" + folder + "/" + base + ".local.slin";
    record.peer = waveRecordPrefix + "/" + folder + "/" + base + ".remote.slin";
    if (!record.dispatch()) {
        Engine.alarm(Engine.DebugWarn, "Cliniva: Yate n'a pas accepté l'enregistrement de l'appel " + pairKey);
        return;
    }
    clinivaRecordedCallPairs[dedupeKey] = true;
    Engine.debug(Engine.DebugInfo, "Cliniva: enregistrement audio démarré; billid=" + billid +
        "; canal=" + localId + "; pair=" + pairKey);
}

if (!Message.install(clinivaRecordAnsweredCall, "call.answered", 90))
    Engine.alarm(Engine.DebugWarn, "Cliniva: impossible d'installer le gestionnaire call.answered");
