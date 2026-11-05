# SPDX-License-Identifier: MIT
{
  pkgs,
  projection,
  library,
  symbol,
}:
pkgs.runCommand "ordinary-native-foreign-consumers"
  {
    nativeBuildInputs = [
      pkgs.python3
      pkgs.jdk
    ];
  }
  ''
    PYTHONPATH=${projection.python-ffi} python - <<'PY'
    import ctypes, json, purifier_native
    library = purifier_native.load('${library}')
    call = getattr(library, '${symbol}')
    call.argtypes = []
    call.restype = ctypes.c_int
    assert call() == 42
    catalog = json.load(open('${projection.native.catalog}'))
    assert catalog['system'] == '${pkgs.stdenv.hostPlatform.system}'
    open('library-path', 'w').write(catalog['libraries']['${library}'])
    PY
    cat > Foreign.java <<'JAVA'
    import java.lang.foreign.*;
    public class Foreign {
      public static void main(String[] args) throws Throwable {
        try (var arena = Arena.ofConfined()) {
          var symbol = SymbolLookup.libraryLookup(args[0], arena).find("${symbol}").orElseThrow();
          var call = Linker.nativeLinker().downcallHandle(symbol, FunctionDescriptor.of(ValueLayout.JAVA_INT));
          if ((int) call.invokeExact() != 42) throw new AssertionError();
        }
      }
    }
    JAVA
    javac --enable-preview --release ${pkgs.lib.versions.major pkgs.jdk.version} Foreign.java
    java --enable-preview --enable-native-access=ALL-UNNAMED Foreign "$(cat library-path)"
    touch $out
  ''
