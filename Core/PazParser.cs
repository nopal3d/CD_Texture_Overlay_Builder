using System.Buffers.Binary;
using System.Text;

namespace CDTextureOverlayBuilder.Core;

public sealed class PazEntry
{
    public string Path { get; init; } = "";
    public string PazFile { get; init; } = "";
    public uint Offset { get; init; }
    public uint CompSize { get; init; }
    public uint OrigSize { get; init; }
    public uint Flags { get; init; }
    public uint PazIndex { get; init; }
    public bool Compressed => CompSize != OrigSize;
    public int CompressionType => (int)((Flags >> 16) & 0x0F);
    public bool Encrypted => Path.EndsWith(".xml", StringComparison.OrdinalIgnoreCase) || Path.EndsWith(".css", StringComparison.OrdinalIgnoreCase) || Path.EndsWith(".html", StringComparison.OrdinalIgnoreCase) || Path.EndsWith(".js", StringComparison.OrdinalIgnoreCase);
}

/// <summary>
/// Low-level PAMT reader for both index layouts:
///  - legacy (game &lt;= 2.03): name-tree records are [u32 absolute parent][u8 len][name],
///    root parent = 0xFFFFFFFF, file names carry their extension.
///  - compact (game 2.04+, "Charting the Unknown"): name-tree records are
///    [varint backwards distance][u8 len][name] (distance 0 = root), file names have
///    no extension; a NUL-separated extension table follows the file records and the
///    extension index is the high byte of the file flags.
/// Folder records (16 bytes) and file records (20 bytes) are unchanged.
/// </summary>
public sealed class PamtData
{
    public const uint NoParent = 0xFFFFFFFF;

    public bool Compact { get; private set; }
    public uint PazCount { get; private set; }
    public Dictionary<uint, (uint parent, string name)> Folders { get; } = new();
    public Dictionary<uint, (uint parent, string name)> Nodes { get; } = new();
    public List<(uint hash, uint folderRef, uint firstFile, uint fileCount)> FolderRecords { get; } = new();
    public List<(uint nodeRef, uint offset, uint compSize, uint origSize, uint flags)> Files { get; } = new();
    public List<string> Extensions { get; } = new();

    /// <summary>The legacy "root folder" prefix (name of the parentless folder node).</summary>
    public string FolderPrefix
    {
        get
        {
            string prefix = "";
            foreach (var f in Folders.Values) if (f.parent == NoParent) prefix = f.name;
            return prefix;
        }
    }

    private const uint MaxSanePazCount = 4096;

    public static PamtData Read(string pamtPath) => Parse(File.ReadAllBytes(pamtPath), Path.GetFileName(pamtPath));

    public static PamtData Parse(byte[] data, string name = "0.pamt")
    {
        try { return ParseImpl(data); }
        catch (InvalidDataException e) { throw new InvalidDataException($"Corrupt PAMT {name}: {e.Message}", e); }
        catch (Exception e) when (e is ArgumentException or IndexOutOfRangeException or OverflowException or DecoderFallbackException)
        {
            throw new InvalidDataException($"Corrupt PAMT {name}: {e.Message}", e);
        }
    }

    private static PamtData ParseImpl(byte[] data)
    {
        if (data.Length < 32) throw new InvalidDataException($"truncated (file is {data.Length} bytes, need at least 32)");
        var p = new PamtData();
        int off = 4; // outer hash
        p.PazCount = BinaryUtil.U32(data, off); off += 4;
        if (p.PazCount > MaxSanePazCount) throw new InvalidDataException($"paz_count {p.PazCount} is implausibly large");
        off += 8; // constant/zero (varies per build)
        for (int i = 0; i < p.PazCount; i++) { off += 8; if (i < p.PazCount - 1) off += 4; }

        uint folderSize = BinaryUtil.U32(data, off); off += 4;
        int folderEnd = checked(off + (int)folderSize);
        if (folderEnd > data.Length) throw new InvalidDataException("folder section extends past end of file");
        bool compact = folderSize > 0 && data[off] == 0;
        ReadNameTree(data, off, folderEnd, compact, p.Folders);
        off = folderEnd;

        uint nodeSize = BinaryUtil.U32(data, off); off += 4;
        int nodeStart = off;
        int nodeEnd = checked(nodeStart + (int)nodeSize);
        if (nodeEnd > data.Length) throw new InvalidDataException("node section extends past end of file");
        if (folderSize == 0 && nodeSize > 0) compact = data[nodeStart] == 0;
        p.Compact = compact;
        ReadNameTree(data, nodeStart, nodeEnd, compact, p.Nodes);
        off = nodeEnd;

        uint folderCount = BinaryUtil.U32(data, off); off += 4;
        if ((long)off + (long)folderCount * 16 > data.Length) throw new InvalidDataException("folder records extend past end of file");
        for (int i = 0; i < folderCount; i++)
        {
            p.FolderRecords.Add((BinaryUtil.U32(data, off), BinaryUtil.U32(data, off + 4), BinaryUtil.U32(data, off + 8), BinaryUtil.U32(data, off + 12)));
            off += 16;
        }

        // 2.04 writes an empty archive as just its (empty) extension table; the
        // zero file-count field is omitted.
        if (p.PazCount == 0 && folderSize == 0 && nodeSize == 0 && folderCount == 0
            && data.Length - off == 5 && data[off] == 1 && data[off + 1] == 0 && data[off + 2] == 0 && data[off + 3] == 0 && data[off + 4] == 0)
        {
            p.Compact = true;
            return p;
        }

        if (off + 4 > data.Length) throw new InvalidDataException("file count missing");
        uint fileCount = BinaryUtil.U32(data, off); off += 4;
        long recordsEnd = off + (long)fileCount * 20;
        if (recordsEnd > data.Length) throw new InvalidDataException("file records extend past end of file");
        for (uint i = 0; i < fileCount; i++)
        {
            p.Files.Add((BinaryUtil.U32(data, off), BinaryUtil.U32(data, off + 4), BinaryUtil.U32(data, off + 8), BinaryUtil.U32(data, off + 12), BinaryUtil.U32(data, off + 16)));
            off += 20;
        }

        if (compact)
        {
            if (off + 4 > data.Length) throw new InvalidDataException("extension table missing");
            uint extSize = BinaryUtil.U32(data, off); off += 4;
            if ((long)off + extSize != data.Length) throw new InvalidDataException("extension table does not tile the file");
            if (extSize == 0 || data[data.Length - 1] != 0) throw new InvalidDataException("unterminated extension table");
            string exts = Encoding.UTF8.GetString(data, off, (int)extSize - 1);
            p.Extensions.AddRange(exts.Split('\0'));
        }
        return p;
    }

    private static void ReadNameTree(byte[] data, int start, int end, bool compact, Dictionary<uint, (uint parent, string name)> nodes)
    {
        int pos = start;
        while (pos < end)
        {
            uint rel = (uint)(pos - start);
            uint parent;
            if (compact)
            {
                uint distance = 0;
                int shift = 0;
                while (true)
                {
                    if (pos >= end) throw new InvalidDataException("truncated parent varint");
                    if (shift > 28) throw new InvalidDataException("oversized parent varint");
                    byte b = data[pos++];
                    distance |= (uint)(b & 0x7F) << shift;
                    shift += 7;
                    if ((b & 0x80) == 0) break;
                }
                if (distance == 0) parent = NoParent;
                else
                {
                    if (distance > rel) throw new InvalidDataException("parent points before the name tree");
                    parent = rel - distance;
                    if (!nodes.ContainsKey(parent)) throw new InvalidDataException("parent is not an earlier tree node");
                }
            }
            else
            {
                if (pos + 4 > end) throw new InvalidDataException("truncated tree parent");
                parent = BinaryUtil.U32(data, pos);
                pos += 4;
            }
            if (pos >= end || pos + 1 + data[pos] > end) throw new InvalidDataException("truncated tree name");
            int len = data[pos++];
            string name = Encoding.UTF8.GetString(data, pos, len);
            pos += len;
            nodes[rel] = (parent, name);
        }
    }

    private static string TreePath(Dictionary<uint, (uint parent, string name)> tree, uint nodeRef)
    {
        var parts = new List<string>();
        uint cur = nodeRef;
        int guard = 0;
        while (cur != NoParent && guard++ < 64)
        {
            if (!tree.TryGetValue(cur, out var n)) break;
            parts.Add(n.name);
            cur = n.parent;
        }
        parts.Reverse();
        return string.Concat(parts);
    }

    public string FolderPath(uint folderRef) => TreePath(Folders, folderRef);

    /// <summary>File name (relative to its folder) including the extension in both layouts.</summary>
    public string FileName(int fileIndex)
    {
        var f = Files[fileIndex];
        string name = TreePath(Nodes, f.nodeRef);
        if (Compact)
        {
            int ext = (int)(f.flags >> 24);
            if (ext >= Extensions.Count) throw new InvalidDataException($"invalid extension index {ext}");
            name += Extensions[ext];
        }
        return name;
    }

    /// <summary>Maps file index -> folder path (from the folder records).</summary>
    public Dictionary<uint, string> FileFolders()
    {
        var map = new Dictionary<uint, string>();
        foreach (var fr in FolderRecords)
        {
            string path = FolderPath(fr.folderRef).Trim('/');
            for (uint k = fr.firstFile; k < fr.firstFile + fr.fileCount; k++) map[k] = path;
        }
        return map;
    }

    /// <summary>
    /// Flags as the rest of the tool expects them: in the compact layout the high byte
    /// is the extension index, which is stripped so compression/crypto bits stay put.
    /// </summary>
    public uint NormalizedFlags(int fileIndex) => Compact ? Files[fileIndex].flags & 0x00FFFFFF : Files[fileIndex].flags;
}

/// <summary>Helpers for detecting the game's archive-index generation.</summary>
public static class GameArchiveLayout
{
    /// <summary>
    /// True when the game is on the 2.04+ layout ("Charting the Unknown" update).
    /// 2.04 signs meta/0.papgt (64-byte trailer) and refuses to start once it has
    /// been rewritten, so overlay installs that add PAPGT entries cannot work there.
    /// </summary>
    public static bool UsesSignedIndex(string gameDir)
    {
        try
        {
            string papgt = Path.Combine(gameDir, "meta", "0.papgt");
            if (File.Exists(papgt) && IsCompactPapgt(File.ReadAllBytes(papgt))) return true;
        }
        catch { }
        try
        {
            string pamt = Path.Combine(gameDir, "0000", "0.pamt");
            if (File.Exists(pamt)) return IsCompactPamt(File.ReadAllBytes(pamt));
        }
        catch { }
        return false;
    }

    public static bool IsCompactPamt(byte[] data)
    {
        if (data.Length < 32) return false;
        uint pazCount = BinaryUtil.U32(data, 4);
        if (pazCount > 4096) return false;
        long off = 16 + pazCount * 8L + Math.Max(0, (long)pazCount - 1) * 4;
        if (off + 5 > data.Length) return false;
        uint folderSize = BinaryUtil.U32(data, (int)off);
        return folderSize > 0 && data[off + 4] == 0;
    }

    /// <summary>2.04 PAPGT: 4-byte prefix, 12-byte entries, u32 name-table size, names, 64-byte trailer.</summary>
    public static bool IsCompactPapgt(byte[] data)
    {
        const int entrySize = 12;
        for (int count = 1; count <= Math.Max(1, (data.Length - 72) / entrySize); count++)
        {
            int sizePos = 4 + count * entrySize;
            if (sizePos + 4 > data.Length) break;
            uint size = BinaryUtil.U32(data, sizePos);
            long start = sizePos + 4;
            long end = start + size;
            if (size == 0 || end + 64 != data.Length) continue;
            bool ok = true;
            var seen = new HashSet<string>();
            for (int i = 0; i < count && ok; i++)
            {
                uint nameOff = BinaryUtil.U32(data, 4 + i * entrySize + 4);
                if (nameOff >= size || (nameOff > 0 && data[start + nameOff - 1] != 0)) { ok = false; break; }
                int abs = (int)(start + nameOff);
                int z = Array.IndexOf(data, (byte)0, abs, (int)(end - abs));
                string name = z >= 0 ? Encoding.ASCII.GetString(data, abs, z - abs) : "";
                if (name.Length != 4 || !name.All(char.IsDigit) || !seen.Add(name)) ok = false;
            }
            if (ok) return true;
        }
        return false;
    }

    public const string SignedIndexMessage =
        "Crimson Desert 2.04 or newer detected (Charting the Unknown update). This game version signs meta/0.papgt " +
        "and refuses to start once it has been modified, so HD overlays cannot be installed into the game yet. " +
        "Nothing was written to the game folder. If a mod tool already changed meta/0.papgt or meta/0.pathc, " +
        "use Steam > Verify integrity of game files to restore them.";

    public static void ThrowIfSignedIndex(string gameDir)
    {
        if (UsesSignedIndex(gameDir)) throw new NotSupportedException(SignedIndexMessage);
    }
}

public static class PazParser
{
    public static List<PazEntry> ParsePamt(string pamtPath, string? pazDir = null)
    {
        var pamt = PamtData.Read(pamtPath);
        pazDir ??= Path.GetDirectoryName(pamtPath) ?? ".";
        string pamtStem = Path.GetFileNameWithoutExtension(pamtPath);
        int stemNum = int.TryParse(pamtStem, out var sn) ? sn : 0;
        string folderPrefix = pamt.FolderPrefix;
        var entries = new List<PazEntry>(pamt.Files.Count);
        for (int i = 0; i < pamt.Files.Count; i++)
        {
            var f = pamt.Files[i];
            string filename = pamt.FileName(i);
            string entryPath = string.IsNullOrEmpty(folderPrefix) ? filename : $"{folderPrefix}/{filename}";
            uint flags = pamt.NormalizedFlags(i);
            // The PAZ chunk id is a u16 (compression type sits at bit 16).
            uint pazIndex = flags & 0xFFFF;
            string pazFile = Path.Combine(pazDir, $"{stemNum + pazIndex}.paz");
            entries.Add(new PazEntry { Path = entryPath.Replace('\\', '/'), PazFile = pazFile, Offset = f.offset, CompSize = f.compSize, OrigSize = f.origSize, Flags = flags, PazIndex = pazIndex });
        }
        return entries;
    }
}
