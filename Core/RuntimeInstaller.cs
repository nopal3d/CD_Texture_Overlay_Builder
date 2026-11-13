using System.Diagnostics;
using System.Reflection;
using System.Text;

namespace CDTextureOverlayBuilder.Core;

/// <summary>
/// Game 2.04+ install path. The game signs meta/0.papgt, so HD overlays cannot be
/// registered as new archive folders any more. Instead the CDHD runtime plugin
/// (bin64\CDHDRuntime.asi, loaded by an ASI loader) serves the matched DDS files
/// straight from disk when the game's pack reader asks for them. This class writes
/// the plugin's manifest, installs the plugin and manages hold/remove/relink.
/// Nothing in the game's archives or meta folder is written.
/// </summary>
public static class RuntimeInstaller
{
    public const string AsiFileName = "CDHDRuntime.asi";
    public const string DataDirName = "cdhd";
    public const int ManifestVersion = 1;
    private const string AsiResourceName = "CDTextureOverlayBuilder.Runtime.CDHDRuntime.asi";
    private static readonly string[] AsiLoaderNames = { "winmm.dll", "version.dll", "dxgi.dll" };

    public static string Bin64(string gameDir) => Path.Combine(gameDir, "bin64");
    public static string DataDir(string gameDir) => Path.Combine(Bin64(gameDir), DataDirName);
    public static string ManifestPath(string gameDir) => Path.Combine(DataDir(gameDir), "manifest.tsv");
    public static string HeldManifestPath(string gameDir) => ManifestPath(gameDir) + ".held";
    public static string LogPath(string gameDir) => Path.Combine(DataDir(gameDir), "CDHDRuntime.log");
    public static string TextureCacheDir(string gameDir) => Path.Combine(DataDir(gameDir), "textures");
    private static string TextureCacheIndex(string gameDir) => Path.Combine(DataDir(gameDir), "textures.index");

    /// <summary>
    /// Converts plain DDS files to the engine's DDS layout and writes them under
    /// bin64\cdhd\textures\(game path). Unchanged sources (same path, size, time and
    /// game format code) are reused from the previous run.
    /// </summary>
    private static Dictionary<MatchedFile, string> PrepareEngineTextures(string gameDir, List<MatchedFile> served, Dictionary<MatchedFile, uint> last4,
        Action<string> log, Action<int, string>? progress, CancellationToken ct, out List<string> failed)
    {
        string cacheRoot = TextureCacheDir(gameDir);
        Directory.CreateDirectory(cacheRoot);
        var oldIndex = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            if (File.Exists(TextureCacheIndex(gameDir)))
                foreach (var line in File.ReadAllLines(TextureCacheIndex(gameDir)))
                {
                    int t = line.IndexOf('\t');
                    if (t > 0) oldIndex[line[..t]] = line[(t + 1)..];
                }
        }
        catch { }

        var result = new System.Collections.Concurrent.ConcurrentDictionary<MatchedFile, string>();
        var newIndex = new System.Collections.Concurrent.ConcurrentDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var fails = new System.Collections.Concurrent.ConcurrentBag<string>();
        int done = 0, reused = 0, converted = 0, dx10Fixed = 0;
        Parallel.ForEach(served, new ParallelOptions { MaxDegreeOfParallelism = Math.Clamp(Environment.ProcessorCount / 2, 1, 6), CancellationToken = ct }, m =>
        {
            string target = OverlayBuilder.Norm(m.FullPath);
            string dst = Path.Combine(cacheRoot, target.Replace('/', Path.DirectorySeparatorChar));
            try
            {
                var src = new FileInfo(m.SourcePath);
                uint code = last4.TryGetValue(m, out var c) ? c : 0;
                string stamp = $"{src.FullName}|{src.Length}|{src.LastWriteTimeUtc.Ticks}|{code}|v2";
                if (oldIndex.TryGetValue(target, out var old) && old == stamp && File.Exists(dst))
                {
                    Interlocked.Increment(ref reused);
                }
                else
                {
                    byte[] content = DdsTools.ToLegacyHeader(File.ReadAllBytes(src.FullName), out bool legacy);
                    if (legacy) Interlocked.Increment(ref dx10Fixed);
                    var (partial, _) = DdsTools.BuildPartialPayload(content);
                    byte[] buf = new byte[Math.Max(content.Length, partial.Length)];
                    Buffer.BlockCopy(partial, 0, buf, 0, partial.Length);
                    if (code == 0) code = DdsTools.GetFormatLast4(content);
                    if (code != 0 && buf.Length >= 128) BinaryUtil.W32(buf, 124, code);
                    Directory.CreateDirectory(Path.GetDirectoryName(dst)!);
                    File.WriteAllBytes(dst, buf);
                    Interlocked.Increment(ref converted);
                }
                newIndex[target] = stamp;
                result[m] = dst;
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception ex) { fails.Add($"{m.RelPath} -> {target}: {ex.Message}"); }
            int n = Interlocked.Increment(ref done);
            if (n % 100 == 0) progress?.Invoke(60 + (int)(25.0 * n / Math.Max(1, served.Count)), $"CONVERT {n}/{served.Count}");
        });

        // Drop cached files that are no longer part of the install.
        var keep = new HashSet<string>(result.Values, StringComparer.OrdinalIgnoreCase);
        int removed = 0;
        foreach (var f in Directory.EnumerateFiles(cacheRoot, "*", SearchOption.AllDirectories).ToList())
            if (!keep.Contains(f)) { try { File.Delete(f); removed++; } catch { } }
        File.WriteAllLines(TextureCacheIndex(gameDir), newIndex.OrderBy(k => k.Key).Select(k => $"{k.Key}\t{k.Value}"));
        log($"Engine-format textures: {converted} converted, {reused} reused, {removed} stale removed (bin64\\{DataDirName}\\textures).");
        if (dx10Fixed > 0) log($"Headers rewritten to the game's DX9 header (DX10 or ATI1/ATI2): {dx10Fixed} (block data unchanged).");
        failed = fails.ToList();
        return new Dictionary<MatchedFile, string>(result);
    }

    public static bool IsInstalled(string gameDir) => File.Exists(ManifestPath(gameDir));
    public static bool IsHeld(string gameDir) => File.Exists(HeldManifestPath(gameDir));

    // ------------------------------------------------------------------ build identity
    public static string BuildLine(string gameDir)
    {
        byte[] paver = File.ReadAllBytes(Path.Combine(gameDir, "meta", "0.paver"));
        long exeSize = new FileInfo(Path.Combine(Bin64(gameDir), "CrimsonDesert.exe")).Length;
        return $"build\t{Convert.ToHexString(paver).ToLowerInvariant()}\t{exeSize}";
    }

    // ------------------------------------------------------------------ DDS format check
    /// <summary>Canonical block-compression family of a DDS header, ignoring sRGB/typeless variants.</summary>
    public static string? DdsFormatFamily(ReadOnlySpan<byte> h)
    {
        if (h.Length < 128 || h[0] != 'D' || h[1] != 'D' || h[2] != 'S' || h[3] != ' ') return null;
        string fourcc = Encoding.ASCII.GetString(h.Slice(84, 4));
        if (fourcc == "DX10")
        {
            if (h.Length < 132) return null;
            uint dxgi = BinaryUtil.U32(h.ToArray(), 128);
            return dxgi switch
            {
                70 or 71 or 72 => "BC1",
                73 or 74 or 75 => "BC2",
                76 or 77 or 78 => "BC3",
                79 or 80 => "BC4U",
                81 => "BC4S",
                82 or 83 => "BC5U",
                84 => "BC5S",
                94 or 95 => "BC6H_UF16",
                96 => "BC6H_SF16",
                97 or 98 or 99 => "BC7",
                _ => $"DXGI{dxgi}",
            };
        }
        return fourcc switch
        {
            "DXT1" => "BC1",
            "DXT2" or "DXT3" => "BC2",
            "DXT4" or "DXT5" => "BC3",
            "ATI1" or "BC4U" => "BC4U",
            "BC4S" => "BC4S",
            "ATI2" or "BC5U" => "BC5U",
            "BC5S" => "BC5S",
            "\0\0\0\0" => $"RGB{BinaryUtil.U32(h.ToArray(), 88)}",
            _ => fourcc.Trim('\0'),
        };
    }

    private static byte[] ReadHead(string path, long offset, int count)
    {
        using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        fs.Seek(offset, SeekOrigin.Begin);
        var buf = new byte[count];
        int got = 0;
        while (got < count)
        {
            int n = fs.Read(buf, got, count - got);
            if (n <= 0) break;
            got += n;
        }
        return got == count ? buf : buf.AsSpan(0, got).ToArray();
    }

    private sealed class GameTextureIndex
    {
        private readonly string _gameDir;
        private readonly Dictionary<string, Dictionary<string, PazEntry>> _byDir = new(StringComparer.OrdinalIgnoreCase);
        public GameTextureIndex(string gameDir) { _gameDir = gameDir; }

        public PazEntry? Find(string pamtDir, string entryPath)
        {
            if (!_byDir.TryGetValue(pamtDir, out var map))
            {
                map = new Dictionary<string, PazEntry>(StringComparer.OrdinalIgnoreCase);
                string pamt = Path.Combine(_gameDir, pamtDir, "0.pamt");
                if (File.Exists(pamt))
                    foreach (var e in PazParser.ParsePamt(pamt)) map[OverlayBuilder.Norm(e.Path)] = e;
                _byDir[pamtDir] = map;
            }
            return map.TryGetValue(OverlayBuilder.Norm(entryPath), out var hit) ? hit : null;
        }

        /// <summary>Full-path lookup (folder/filename) across every base-game folder, for Relink.</summary>
        private Dictionary<string, PazEntry>? _byFullPath;
        public PazEntry? FindFull(string fullPath)
        {
            if (_byFullPath == null)
            {
                _byFullPath = new Dictionary<string, PazEntry>(StringComparer.OrdinalIgnoreCase);
                foreach (var dir in Directory.EnumerateDirectories(_gameDir, "????").OrderBy(x => x))
                {
                    string pamt = Path.Combine(dir, "0.pamt");
                    if (!File.Exists(pamt)) continue;
                    try
                    {
                        var data = PamtData.Read(pamt);
                        var folders = data.FileFolders();
                        var entries = PazParser.ParsePamt(pamt);
                        for (int i = 0; i < entries.Count && i < data.Files.Count; i++)
                        {
                            if (!entries[i].Path.EndsWith(".dds", StringComparison.OrdinalIgnoreCase)) continue;
                            string name = data.FileName(i).Trim('/');
                            string full = folders.TryGetValue((uint)i, out var f) && f.Length > 0 ? $"{f}/{name}" : name;
                            _byFullPath.TryAdd(OverlayBuilder.Norm(full), entries[i]);
                        }
                    }
                    catch { }
                }
            }
            return _byFullPath.TryGetValue(OverlayBuilder.Norm(fullPath), out var hit) ? hit : null;
        }
    }

    private enum FormatCheck { Same, Different, Unknown }

    private static FormatCheck CompareFormats(PazEntry? game, string sourcePath, out string detail) => CompareFormats(game, sourcePath, out detail, out _, out _, out _);

    private static FormatCheck CompareFormats(PazEntry? game, string sourcePath, out string detail, out uint gameLast4, out string? gameFamily, out uint gameMips)
    {
        detail = "";
        gameLast4 = 0;
        gameMips = 0;
        gameFamily = null;
        string? modFmt = null, gameFmt = null;
        try { modFmt = DdsFormatFamily(ReadHead(sourcePath, 0, 148)); } catch (Exception ex) { detail = $"cannot read source DDS: {ex.Message}"; return FormatCheck.Unknown; }
        if (modFmt == null) { detail = "source file is not a DDS"; return FormatCheck.Different; }
        if (game == null) { detail = $"game entry not found (mod {modFmt})"; return FormatCheck.Unknown; }
        try
        {
            byte[] gh = ReadHead(game.PazFile, game.Offset, 148);
            gameFmt = DdsFormatFamily(gh);
            if (gh.Length >= 128) { gameLast4 = BinaryUtil.U32(gh, 124); gameMips = Math.Max(1u, BinaryUtil.U32(gh, 28)); }
        }
        catch { }
        if (gameFmt == null) { detail = $"game header unreadable (mod {modFmt})"; return FormatCheck.Unknown; }
        gameFamily = gameFmt;
        detail = $"game {gameFmt}, mod {modFmt}";
        return gameFmt == modFmt ? FormatCheck.Same : FormatCheck.Different;
    }

    /// <summary>True when the DDS has fewer mips than its full chain down to 4x4 blocks.</summary>
    private static bool MissingMips(string path, out string detail)
    {
        detail = "";
        try
        {
            byte[] h = ReadHead(path, 0, 128);
            if (h.Length < 128) return false;
            uint height = Math.Max(1, BinaryUtil.U32(h, 12)), width = Math.Max(1, BinaryUtil.U32(h, 16));
            uint mips = BinaryUtil.U32(h, 28);
            if ((BinaryUtil.U32(h, 8) & 0x20000) == 0 || mips == 0) mips = 1; // DDSD_MIPMAPCOUNT not set
            uint full = 1;
            for (uint d = Math.Max(width, height); d > 4; d >>= 1) full++; // down to 4x4 is enough for block formats
            if (mips >= full) return false;
            detail = $"mod has {mips} mip(s), full chain is {full} -> mipmaps rebuilt";
            return true;
        }
        catch { return false; }
    }

    // ------------------------------------------------------------------ re-encoding (texconv)
    public static string ReencodeCacheDir(string gameDir) => Path.Combine(DataDir(gameDir), "reencoded");

    private static string? TexconvFormat(string family) => family switch
    {
        "BC1" => "BC1_UNORM",
        "BC2" => "BC2_UNORM",
        "BC3" => "BC3_UNORM",
        "BC4U" => "BC4_UNORM",
        "BC4S" => "BC4_SNORM",
        "BC5U" => "BC5_UNORM",
        "BC5S" => "BC5_SNORM",
        "BC7" => "BC7_UNORM",
        _ => null,
    };

    /// <summary>texconv.exe next to the tool, on PATH, or in CDHD_TEXCONV.</summary>
    public static string? FindTexconv()
    {
        var candidates = new List<string>();
        string? env = Environment.GetEnvironmentVariable("CDHD_TEXCONV");
        if (!string.IsNullOrWhiteSpace(env)) candidates.Add(env);
        candidates.Add(Path.Combine(AppContext.BaseDirectory, "texconv.exe"));
        candidates.Add(Path.Combine(AppContext.BaseDirectory, "tools", "texconv.exe"));
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try { candidates.Add(Path.Combine(dir.Trim('"'), "texconv.exe")); } catch { }
        }
        return candidates.FirstOrDefault(File.Exists) ?? ExtractBundledTexconv();
    }

    private static MatchedFile CloneWithSource(MatchedFile m, string source) => new()
    {
        SourcePath = source, RelPath = m.RelPath, Size = new FileInfo(source).Length, PamtDir = m.PamtDir, EntryPath = m.EntryPath,
        FullPath = m.FullPath, Filename = m.Filename, CompressionType = m.CompressionType, Encrypted = m.Encrypted,
        CryptoFilename = m.CryptoFilename, MatchMethod = m.MatchMethod,
    };

    /// <summary>
    /// Re-encodes textures whose block format differs from the game's into the game's
    /// format with texconv, keeping their own size and mip chain. Results are cached in
    /// bin64\cdhd\reencoded and reused while the source is unchanged.
    /// </summary>
    private static List<(MatchedFile m, string? newSource, uint last4, string detail)> ReencodeToGameFormat(string gameDir,
        List<(MatchedFile m, string family, uint last4, string detail)> items, Action<string> log, CancellationToken ct)
    {
        var results = new List<(MatchedFile, string?, uint, string)>();
        string? texconv = FindTexconv();
        if (texconv == null)
        {
            log($"NOTICE: {items.Count} texture(s) need re-encoding to the game's format, but texconv.exe was not found. " +
                "This build does not bundle texconv; put texconv.exe next to HD Overlay Builder.exe (github.com/microsoft/DirectXTex/releases) and apply again.");
            foreach (var it in items) results.Add((it.m, null, it.last4, it.detail + " (texconv.exe not found)"));
            return results;
        }
        string root = ReencodeCacheDir(gameDir);
        int converted = 0, reused = 0;
        foreach (var it in items)
        {
            ct.ThrowIfCancellationRequested();
            string fmt = TexconvFormat(it.family)!;
            string target = OverlayBuilder.Norm(it.m.FullPath);
            string outDir = Path.Combine(root, Path.GetDirectoryName(target.Replace('/', Path.DirectorySeparatorChar)) ?? "");
            string outFile = Path.Combine(outDir, Path.GetFileName(it.m.SourcePath));
            var src = new FileInfo(it.m.SourcePath);
            string stampFile = outFile + ".src";
            string stamp = $"{src.FullName}|{src.Length}|{src.LastWriteTimeUtc.Ticks}|{fmt}|v2";
            try
            {
                if (File.Exists(outFile) && File.Exists(stampFile) && File.ReadAllText(stampFile) == stamp)
                {
                    reused++;
                    results.Add((it.m, outFile, it.last4, it.detail + $" -> {it.family}"));
                    continue;
                }
                Directory.CreateDirectory(outDir);
                var args = new List<string> { "-nologo", "-y", "-f", fmt, "-m", "0", "-o", outDir };
                // The game's DDS headers are legacy DX9 for BC1-BC5; BC6H/BC7 need the DX10 header.
                args.Add(it.family is "BC7" ? "-dx10" : "-dx9");
                if (it.family == "BC1") { args.Add("-at"); args.Add("0.5"); }
                args.Add("--");
                args.Add(src.FullName);
                var psi = new ProcessStartInfo(texconv) { UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true };
                foreach (var a in args) psi.ArgumentList.Add(a);
                using var proc = Process.Start(psi)!;
                string stdout = proc.StandardOutput.ReadToEnd() + proc.StandardError.ReadToEnd();
                proc.WaitForExit();
                string? fam = File.Exists(outFile) ? DdsFormatFamily(ReadHead(outFile, 0, 148)) : null;
                if (proc.ExitCode != 0 || fam != it.family)
                {
                    string why = proc.ExitCode != 0 ? $"texconv failed ({proc.ExitCode}): {stdout.Trim().Split('\n').LastOrDefault()}" : $"texconv produced {fam ?? "nothing"}";
                    results.Add((it.m, null, it.last4, it.detail + "; " + why));
                    continue;
                }
                File.WriteAllText(stampFile, stamp);
                converted++;
                results.Add((it.m, outFile, it.last4, it.detail + $" -> {it.family}"));
            }
            catch (Exception ex)
            {
                results.Add((it.m, null, it.last4, it.detail + "; " + ex.Message));
            }
        }
        log($"Re-encoded to the game's format with texconv: {converted} converted, {reused} reused ({texconv}).");
        return results;
    }

    // ------------------------------------------------------------------ plugin + loader
    public static string? FindAsiLoader(string gameDir)
    {
        foreach (var name in AsiLoaderNames)
        {
            string p = Path.Combine(Bin64(gameDir), name);
            if (!File.Exists(p)) continue;
            try
            {
                byte[] b = File.ReadAllBytes(p);
                if (IndexOf(b, Encoding.ASCII.GetBytes("IsUltimateASILoader")) < 0 && IndexOf(b, Encoding.ASCII.GetBytes("Ultimate-ASI-Loader")) < 0) continue;
                if (!IsX64Pe(b)) { WrongArchLoader = name; continue; }
                return name;
            }
            catch { }
        }
        return null;
    }

    /// <summary>Set by FindAsiLoader when a 32-bit Ultimate ASI Loader was found (the 64-bit game ignores it).</summary>
    public static string? WrongArchLoader { get; private set; }

    private static bool IsX64Pe(byte[] b)
    {
        if (b.Length < 0x40) return false;
        int pe = BitConverter.ToInt32(b, 0x3C);
        return pe > 0 && pe + 6 <= b.Length && BitConverter.ToUInt16(b, pe + 4) == 0x8664;
    }

    private static int IndexOf(byte[] hay, byte[] needle)
    {
        for (int i = 0; i <= hay.Length - needle.Length; i++)
        {
            if (hay[i] != needle[0]) continue;
            int k = 1;
            while (k < needle.Length && hay[i + k] == needle[k]) k++;
            if (k == needle.Length) return i;
        }
        return -1;
    }

    private static byte[]? EmbeddedAsi()
    {
        using var s = Assembly.GetExecutingAssembly().GetManifestResourceStream(AsiResourceName);
        if (s == null) return null;
        using var ms = new MemoryStream();
        s.CopyTo(ms);
        return ms.ToArray();
    }

    /// <summary>Installs or updates bin64\CDHDRuntime.asi. Returns false if the plugin binary is not available.</summary>
    public static bool InstallPlugin(string gameDir, Action<string> log)
    {
        byte[]? asi = EmbeddedAsi();
        if (asi == null)
        {
            string side = Path.Combine(AppContext.BaseDirectory, AsiFileName);
            if (File.Exists(side)) asi = File.ReadAllBytes(side);
        }
        if (asi == null)
        {
            log("ERROR: CDHDRuntime.asi is not bundled with this build of the tool. Build Runtime\\build.bat and rebuild the tool.");
            return false;
        }
        string dst = Path.Combine(Bin64(gameDir), AsiFileName);
        if (File.Exists(dst) && File.ReadAllBytes(dst).AsSpan().SequenceEqual(asi))
        {
            log($"Runtime plugin is up to date: bin64\\{AsiFileName}");
            return true;
        }
        try
        {
            OverlayService.SafeWrite(dst, asi);
            log($"Runtime plugin installed: bin64\\{AsiFileName}");
            return true;
        }
        catch (IOException ex)
        {
            log($"ERROR: could not write bin64\\{AsiFileName} ({ex.Message}). Close the game and try again.");
            return false;
        }
    }

    private static byte[]? EmbeddedResource(string name)
    {
        using var s = Assembly.GetExecutingAssembly().GetManifestResourceStream(name);
        if (s == null) return null;
        using var ms = new MemoryStream();
        s.CopyTo(ms);
        return ms.ToArray();
    }

    /// <summary>
    /// Makes sure a 64-bit Ultimate ASI Loader is in bin64 so the game loads CDHDRuntime.asi.
    /// Installs the bundled one as winmm.dll when none is present, replaces a 32-bit copy
    /// (kept as .win32.bak), and never overwrites an unrelated DLL another mod placed there.
    /// </summary>
    private static void EnsureAsiLoader(string gameDir, Action<string> log)
    {
        WrongArchLoader = null;
        string? loader = FindAsiLoader(gameDir);
        if (loader != null)
        {
            log($"ASI loader found: bin64\\{loader}");
            return;
        }
        byte[]? ual = EmbeddedResource("CDTextureOverlayBuilder.ThirdParty.UltimateASILoader_x64.dll");
        string? slot = null;
        if (WrongArchLoader != null)
        {
            string old = Path.Combine(Bin64(gameDir), WrongArchLoader);
            if (ual != null)
            {
                File.Move(old, old + ".win32.bak", true);
                log($"bin64\\{WrongArchLoader} was the 32-bit ASI loader (the 64-bit game ignores it); kept as {WrongArchLoader}.win32.bak.");
                slot = WrongArchLoader;
            }
        }
        else
        {
            slot = AsiLoaderNames.Take(2).FirstOrDefault(n => !File.Exists(Path.Combine(Bin64(gameDir), n)));
        }
        if (ual != null && slot != null)
        {
            try
            {
                OverlayService.SafeWrite(Path.Combine(Bin64(gameDir), slot), ual);
                log($"Ultimate ASI Loader (x64) installed as bin64\\{slot}.");
                return;
            }
            catch (IOException ex) { log($"ERROR: could not write bin64\\{slot} ({ex.Message}). Close the game and try again."); }
        }
        log("WARNING: no 64-bit ASI loader in bin64 and none could be installed automatically" +
            (ual == null ? " (not bundled in this build)." : " (winmm.dll and version.dll already belong to other software)."));
        log("  Install Ultimate ASI Loader x64 (github.com/ThirteenAG/Ultimate-ASI-Loader/releases, Ultimate-ASI-Loader_x64.zip):");
        log("  put its dinput8.dll in bin64 renamed to winmm.dll (dinput8.dll loads too late for this game).");
    }

    /// <summary>Extracts the bundled texconv.exe to the app data folder.</summary>
    private static string? ExtractBundledTexconv()
    {
        byte[]? exe = EmbeddedResource("CDTextureOverlayBuilder.ThirdParty.texconv.exe");
        if (exe == null) return null;
        string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HDOverlayBuilder", "tools");
        string path = Path.Combine(dir, "texconv.exe");
        try
        {
            if (!File.Exists(path) || new FileInfo(path).Length != exe.Length || !File.ReadAllBytes(path).AsSpan().SequenceEqual(exe))
            {
                Directory.CreateDirectory(dir);
                File.WriteAllBytes(path, exe);
            }
            return path;
        }
        catch { return File.Exists(path) ? path : null; }
    }

    // ------------------------------------------------------------------ apply
    public static BuildResult Apply(BuildOptions options, List<MatchedFile> matches, int notFound, int duplicateIgnored, int failedSkipped, int ambiguous,
        Action<string> log, Action<int, string>? progress, CancellationToken ct, DateTime start)
    {
        string gameDir = options.GameDir;
        log("== Runtime install (game 2.04+) ==");
        log("This game version signs its archive list, so textures are served at runtime by CDHDRuntime.asi. The game's archives and meta folder are not modified.");
        if (Process.GetProcessesByName("CrimsonDesert").Length > 0)
            throw new InvalidOperationException("Crimson Desert is running. Close the game before installing textures.");

        var index = new GameTextureIndex(gameDir);
        var served = new List<MatchedFile>();
        var servedLast4 = new Dictionary<MatchedFile, uint>();
        var toReencode = new List<(MatchedFile m, string family, uint last4, string detail)>();
        var reencoded = new List<string>();
        var formatMismatch = new List<string>();
        var unknownFormat = new List<string>();
        var badPath = new List<string>();
        var seenTargets = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        for (int i = 0; i < matches.Count; i++)
        {
            ct.ThrowIfCancellationRequested();
            if (i % 200 == 0) progress?.Invoke(20 + (int)(40.0 * i / Math.Max(1, matches.Count)), $"CHECK {i}/{matches.Count}");
            var m = matches[i];
            string target = m.FullPath.Replace('\\', '/').Trim('/');
            string source = Path.GetFullPath(m.SourcePath);
            if (target.Length == 0 || target.IndexOfAny(new[] { '\t', '\n', '\r' }) >= 0 || source.IndexOfAny(new[] { '\t', '\n', '\r' }) >= 0)
            {
                badPath.Add($"{m.RelPath} -> {target}");
                continue;
            }
            if (!seenTargets.Add(target)) continue;
            var check = CompareFormats(index.Find(m.PamtDir, m.EntryPath), source, out string detail, out uint gameLast4, out string? gameFamily, out uint gameMips);
            // A mip chain shorter than the game's (often 1 mip from some exporters) shows
            // shimmering stripes/moire at distance: rebuild the full chain with texconv.
            if (check == FormatCheck.Same && gameMips > 1 && gameFamily != null && TexconvFormat(gameFamily) != null
                && MissingMips(source, out string mipDetail))
            {
                check = FormatCheck.Different;
                detail += "; " + mipDetail;
            }
            if (check == FormatCheck.Different)
            {
                if (gameFamily != null && TexconvFormat(gameFamily) != null) toReencode.Add((m, gameFamily, gameLast4, detail));
                else formatMismatch.Add($"{m.RelPath} -> {target}: {detail} (no automatic conversion for this format)");
                continue;
            }
            if (check == FormatCheck.Unknown) unknownFormat.Add($"{m.RelPath} -> {target}: {detail}");
            served.Add(m);
            servedLast4[m] = gameLast4;
        }

        if (toReencode.Count > 0)
        {
            progress?.Invoke(58, "RE-ENCODE");
            foreach (var r in ReencodeToGameFormat(gameDir, toReencode, log, ct))
            {
                if (r.newSource == null) { formatMismatch.Add($"{r.m.RelPath} -> {r.m.FullPath}: {r.detail}"); continue; }
                var copy = CloneWithSource(r.m, r.newSource);
                served.Add(copy);
                servedLast4[copy] = r.last4;
                reencoded.Add($"{r.m.RelPath} -> {r.m.FullPath}: {r.detail}");
            }
        }

        // Game DDS files are not plain DDS: the engine keeps the first mip sizes
        // (first mip LZ4-compressed) in header bytes 0x20-0x2C and a format code at
        // 0x7C. Plain DDS files decode as garbage, so every texture is converted to
        // the engine layout (same transform the pre-2.04 overlay build used) and
        // cached on the game drive.
        progress?.Invoke(60, "CONVERT");
        var servedPath = PrepareEngineTextures(gameDir, served, servedLast4, log, progress, ct, out var convertFailed);
        if (convertFailed.Count > 0)
        {
            served = served.Where(servedPath.ContainsKey).ToList();
            log($"NOTICE: {convertFailed.Count} texture(s) could not be converted and were left out (see report).");
            foreach (var x in convertFailed.Take(10)) log("  " + x);
        }

        progress?.Invoke(85, "MANIFEST");
        var sb = new StringBuilder();
        sb.Append("# Written by HD Overlay Builder. CDHDRuntime.asi serves these files to the game.\n");
        sb.Append($"# {DateTime.Now:yyyy-MM-dd HH:mm:ss}  mod: {options.ModName.Replace('\n', ' ').Replace('\r', ' ')}\n");
        sb.Append($"cdhd-runtime\t{ManifestVersion}\n");
        sb.Append(BuildLine(gameDir)).Append('\n');
        foreach (var m in served)
            sb.Append("file\t").Append(m.FullPath.Replace('\\', '/').Trim('/')).Append('\t').Append(servedPath[m]).Append('\n');

        Directory.CreateDirectory(DataDir(gameDir));
        bool pluginOk = InstallPlugin(gameDir, log);
        if (File.Exists(HeldManifestPath(gameDir))) File.Delete(HeldManifestPath(gameDir));
        OverlayService.SafeWrite(ManifestPath(gameDir), new UTF8Encoding(false).GetBytes(sb.ToString()));
        log($"Runtime manifest written: bin64\\{DataDirName}\\manifest.tsv ({served.Count} texture(s)).");
        EnsureAsiLoader(gameDir, log);

        if (formatMismatch.Count > 0)
        {
            log($"NOTICE: {formatMismatch.Count} texture(s) were NOT installed because their compression format differs from the game's texture and could not be re-encoded automatically. " +
                "Game 2.04 keeps drawing each texture in its original format. Re-export them in the format listed in the report (resolution may stay larger).");
            foreach (var x in formatMismatch.Take(15)) log("  " + x);
        }
        if (unknownFormat.Count > 0) log($"NOTICE: {unknownFormat.Count} texture(s) could not be format-checked and were installed anyway (see report).");

        string stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        string reportDir = Path.Combine(options.OutputDir, $"{OverlayService.SafeName(options.ModName)}_{stamp}_runtime");
        Directory.CreateDirectory(reportDir);
        string reportPath = Path.Combine(reportDir, "runtime_report.txt");
        var rep = new StringBuilder();
        rep.AppendLine("HD Overlay Builder - runtime install report (game 2.04+)");
        rep.AppendLine($"Game: {gameDir}");
        rep.AppendLine($"Textures served: {served.Count}");
        rep.AppendLine($"Format mismatches (not installed): {formatMismatch.Count}");
        rep.AppendLine($"Not format-checked (installed): {unknownFormat.Count}");
        rep.AppendLine($"Invalid paths (not installed): {badPath.Count}");
        rep.AppendLine($"Not found in game: {notFound}; ambiguous: {ambiguous}");
        rep.AppendLine();
        rep.AppendLine("Check after launching the game: bin64\\cdhd\\CDHDRuntime.log should say 'serving'.");
        rep.AppendLine("Create an empty file bin64\\cdhd\\verbose to log every texture the game loads from this list.");
        void Section(string title, List<string> items) { if (items.Count == 0) return; rep.AppendLine(); rep.AppendLine($"== {title} =="); foreach (var x in items) rep.AppendLine(x); }
        Section("FORMAT MISMATCH (re-export in the game's format)", formatMismatch);
        Section("NOT FORMAT-CHECKED", unknownFormat);
        Section("INVALID PATH", badPath);
        Section("CONVERSION FAILED", convertFailed);
        Section("RE-ENCODED TO THE GAME'S FORMAT (texconv)", reencoded);
        rep.AppendLine();
        rep.AppendLine("== SERVED ==");
        foreach (var m in served) rep.AppendLine($"{m.FullPath}\t{m.SourcePath}");
        File.WriteAllText(reportPath, rep.ToString(), Encoding.UTF8);
        File.Copy(ManifestPath(gameDir), Path.Combine(reportDir, "manifest.tsv"), true);

        progress?.Invoke(100, "DONE");
        log(pluginOk
            ? "Runtime install finished. Launch the game and check bin64\\cdhd\\CDHDRuntime.log."
            : "Runtime manifest written, but the plugin could not be installed (see above).");
        return new BuildResult(served.Count, formatMismatch.Count + badPath.Count, ambiguous, new List<string>(), reportDir, ManifestPath(gameDir), reportPath,
            pluginOk, (DateTime.UtcNow - start).TotalSeconds, notFound, duplicateIgnored, failedSkipped + formatMismatch.Count + badPath.Count);
    }

    // ------------------------------------------------------------------ remove / hold / release / relink
    public static bool Remove(string gameDir, Action<string> log)
    {
        if (Process.GetProcessesByName("CrimsonDesert").Length > 0)
            throw new InvalidOperationException("Crimson Desert is running and has the served textures open. Close the game, then remove the build.");
        bool any = false;
        try
        {
            if (Directory.Exists(TextureCacheDir(gameDir))) { Directory.Delete(TextureCacheDir(gameDir), true); any = true; }
            if (File.Exists(TextureCacheIndex(gameDir))) File.Delete(TextureCacheIndex(gameDir));
            if (Directory.Exists(ReencodeCacheDir(gameDir))) Directory.Delete(ReencodeCacheDir(gameDir), true);
        }
        catch (Exception ex) { log($"WARN: could not delete {TextureCacheDir(gameDir)}: {ex.Message}"); }
        foreach (var p in new[] { ManifestPath(gameDir), HeldManifestPath(gameDir) })
        {
            if (!File.Exists(p)) continue;
            File.Delete(p);
            any = true;
        }
        log(any
            ? "Runtime textures removed: the manifest was deleted, the game loads its own textures again. (bin64\\CDHDRuntime.asi stays and does nothing without a manifest.)"
            : "No runtime texture install was found.");
        return any;
    }

    public static void Hold(string gameDir, Action<string> log)
    {
        if (!File.Exists(ManifestPath(gameDir))) { log("Hold: no active runtime texture install."); return; }
        File.Move(ManifestPath(gameDir), HeldManifestPath(gameDir), true);
        log("Hold: runtime textures paused (manifest.tsv -> manifest.tsv.held). The game loads its own textures until Release.");
    }

    public static void Release(string gameDir, Action<string> log)
    {
        if (!File.Exists(HeldManifestPath(gameDir))) { log("Release: no held runtime texture install."); return; }
        File.Move(HeldManifestPath(gameDir), ManifestPath(gameDir), true);
        log("Release: runtime textures active again.");
        RelinkManifest(gameDir, log);
    }

    /// <summary>After a game update: re-stamp the manifest for the new build and drop entries whose format no longer matches.</summary>
    public static BuildResult Relink(string gameDir, Action<string> log, Action<int, string>? progress)
    {
        var start = DateTime.UtcNow;
        progress?.Invoke(5, "RELINK");
        var (kept, dropped) = RelinkManifest(gameDir, log);
        progress?.Invoke(100, "RELINK DONE");
        return new BuildResult(kept, dropped, 0, new List<string>(), DataDir(gameDir), ManifestPath(gameDir), LogPath(gameDir), kept > 0,
            (DateTime.UtcNow - start).TotalSeconds, 0, 0, dropped);
    }

    private static (int kept, int dropped) RelinkManifest(string gameDir, Action<string> log)
    {
        string path = File.Exists(ManifestPath(gameDir)) ? ManifestPath(gameDir) : HeldManifestPath(gameDir);
        if (!File.Exists(path)) throw new InvalidOperationException("No runtime texture install to relink. Use Easy Apply to install textures.");
        if (Process.GetProcessesByName("CrimsonDesert").Length > 0)
            throw new InvalidOperationException("Crimson Desert is running. Close the game first.");
        var index = new GameTextureIndex(gameDir);
        var outLines = new List<string>();
        int kept = 0, dropped = 0;
        foreach (var raw in File.ReadAllLines(path, Encoding.UTF8))
        {
            var f = raw.Split('\t');
            if (f.Length >= 2 && f[0] == "build") { outLines.Add(BuildLine(gameDir)); continue; }
            if (f.Length >= 3 && f[0] == "file")
            {
                var check = CompareFormats(index.FindFull(f[1]), f[2], out string detail);
                if (check == FormatCheck.Different || !File.Exists(f[2]))
                {
                    dropped++;
                    log($"Relink: dropped {f[1]} ({(File.Exists(f[2]) ? detail : "source file missing")})");
                    continue;
                }
                kept++;
            }
            outLines.Add(raw);
        }
        OverlayService.SafeWrite(path, new UTF8Encoding(false).GetBytes(string.Join("\n", outLines) + "\n"));
        log($"Relink: manifest stamped for the current game build. Textures kept: {kept}, dropped: {dropped}.");
        EnsureAsiLoader(gameDir, log);
        InstallPlugin(gameDir, log);
        return (kept, dropped);
    }
}
