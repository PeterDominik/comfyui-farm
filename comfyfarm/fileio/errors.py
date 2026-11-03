"""The two errors fileio raises. Their messages are shown to the artist as they are."""


class SequenceError(ValueError):
    """The input can't be used; the message is shown to the artist as-is."""


class ConvertError(RuntimeError):
    """Conversion failed while the job was running; the message is shown to the artist."""
