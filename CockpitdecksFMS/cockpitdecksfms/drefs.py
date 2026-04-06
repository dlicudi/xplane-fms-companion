"""DrefsMixin — XPPython3 dataref and command registration helpers."""

from XPPython3 import xp


class DrefsMixin:
    """Mixin providing low-level dataref and command registration for all subsystems."""

    def _register_string_dref(self, suffix: str):
        name = f"{self.DREF_PREFIX}/{suffix}"

        def read_data(refCon, values, offset, count):
            text = self.string_values.get(suffix, "")
            data = list(text.encode("utf-8"))
            if values is None:
                return len(data)
            if offset >= len(data):
                return 0
            values.extend(data[offset: offset + count])
            return min(count, len(data) - offset)

        accessor = xp.registerDataAccessor(
            name,
            dataType=xp.Type_Data,
            writable=0,
            readData=read_data,
            readRefCon=suffix,
        )
        self.accessors.append(accessor)
        self._log("Registered string dataref", name, "->", accessor)

    def _register_int_dref(self, suffix: str):
        name = f"{self.DREF_PREFIX}/{suffix}"

        def read_int(refCon):
            return int(self.int_values.get(suffix, 0))

        accessor = xp.registerDataAccessor(
            name,
            dataType=xp.Type_Int,
            writable=0,
            readInt=read_int,
            readRefCon=suffix,
        )
        self.accessors.append(accessor)
        self._log("Registered int dataref", name, "->", accessor)

    def _register_float_dref(self, suffix: str):
        name = f"{self.DREF_PREFIX}/{suffix}"

        def read_float(refCon):
            return float(self.float_values.get(suffix, 0.0))

        accessor = xp.registerDataAccessor(
            name,
            dataType=xp.Type_Float,
            writable=0,
            readFloat=read_float,
            readRefCon=suffix,
        )
        self.accessors.append(accessor)
        self._log("Registered float dataref", name, "->", accessor)

    def _register_writable_action_dref(self, suffix: str):
        name = f"{self.DREF_PREFIX}/{suffix}"

        def read_int(refCon):
            return int(self.int_values.get(suffix, 0))

        def write_int(refCon, value):
            try:
                action = int(value)
            except Exception:
                action = 0
            self._log("Action dataref write", name, "=", action)
            self.int_values[suffix] = action
            self._perform_action(action)
            self.int_values[suffix] = 0

        accessor = xp.registerDataAccessor(
            name,
            dataType=xp.Type_Int,
            writable=1,
            readInt=read_int,
            writeInt=write_int,
            readRefCon=suffix,
            writeRefCon=suffix,
        )
        self.accessors.append(accessor)
        self._log("Registered writable action dataref", name, "->", accessor)

    def _register_live_int_dref(self, suffix: str, read_fn, prefix: str = None):
        name = f"{prefix or self.DREF_PREFIX}/{suffix}"

        def read_int(refCon):
            return read_fn()

        accessor = xp.registerDataAccessor(
            name,
            dataType=xp.Type_Int,
            writable=0,
            readInt=read_int,
            readRefCon=suffix,
        )
        self.accessors.append(accessor)
        self._log("Registered live int dataref", name, "->", accessor)

    def _register_live_float_dref(self, suffix: str, read_fn, prefix: str = None):
        name = f"{prefix or self.DREF_PREFIX}/{suffix}"

        def read_float(refCon):
            return float(read_fn())

        accessor = xp.registerDataAccessor(
            name,
            dataType=xp.Type_Float,
            writable=0,
            readFloat=read_float,
            readRefCon=suffix,
        )
        self.accessors.append(accessor)
        self._log("Registered live float dataref", name, "->", accessor)

    def _register_live_string_dref(self, suffix: str, read_fn, prefix: str = None):
        name = f"{prefix or self.DREF_PREFIX}/{suffix}"

        def read_data(refCon, values, offset, count):
            text = read_fn()
            data = list(text.encode("utf-8"))
            if values is None:
                return len(data)
            if offset >= len(data):
                return 0
            values.extend(data[offset: offset + count])
            return min(count, len(data) - offset)

        accessor = xp.registerDataAccessor(
            name,
            dataType=xp.Type_Data,
            writable=0,
            readData=read_data,
            readRefCon=suffix,
        )
        self.accessors.append(accessor)
        self._log("Registered live string dataref", name, "->", accessor)

    def _create_command(self, suffix: str, desc: str, callback, prefix: str = None):
        name = f"{prefix or self.CMD_PREFIX}/{suffix}"
        cmd_ref = xp.createCommand(name, desc)
        self._log("createCommand", name, "->", cmd_ref)
        if not cmd_ref:
            self._log("ERROR: command creation failed for", name)
            return

        def handler(commandRef, phase, refcon):
            if phase == xp.CommandBegin:
                self._log("Command begin", name)
                callback()
            return 1

        xp.registerCommandHandler(cmd_ref, handler, 1, None)
        self._log("registerCommandHandler", name, "-> OK")
        self.commands[name] = {"ref": cmd_ref, "fun": handler}
