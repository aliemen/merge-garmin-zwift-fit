from garmin_fit_sdk import Decoder, Stream


def decode_fit(path):
    """Decode a FIT file from disk.

    Returns (messages, errors, definitions).

    `definitions` is a dict mapping global_mesg_num -> the message definition
    record the source file embedded. The garmin-fit-sdk's bundled Profile
    doesn't know about Garmin's proprietary message types (140, 141, 233, …)
    that hold metrics like Performance Condition, Body Battery, sweat loss,
    etc. — but every FIT file embeds the definitions for the messages it
    uses, so we capture those here and let the encoder synthesize Profile
    entries on the fly to pass them through.

    The Stream is single-pass, so callers must NOT pre-call
    is_fit()/check_integrity() before read() — those exhaust the stream and
    read() will silently return an empty dict.
    """
    captured = {}

    def _capture(defn):
        # FIT files MAY redefine a message mid-stream with different fields
        # (Garmin's exporters do this — e.g., a later record definition adds
        # `field_90` for a subset of records). Merge field definitions across
        # all redefinitions of a mesg_num so the encode-side Profile patch
        # covers the union.
        num = defn["global_mesg_num"]
        existing = captured.get(num)
        if existing is None:
            captured[num] = dict(defn)
            captured[num]["field_definitions"] = list(defn["field_definitions"])
            return
        seen = {fd["field_id"] for fd in existing["field_definitions"]}
        for fd in defn["field_definitions"]:
            if fd["field_id"] not in seen:
                existing["field_definitions"].append(fd)
                seen.add(fd["field_id"])

    stream = Stream.from_file(path)
    decoder = Decoder(stream)
    messages, errors = decoder.read(mesg_definition_listener=_capture)
    return messages, errors, captured
