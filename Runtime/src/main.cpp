// CDHD Runtime - serves loose files (HD textures) to Crimson Desert 2.04+
// without modifying the game's signed archives.
//
// Mechanism (same idea the DMM runtime uses, independent implementation):
//   * find pa::BaseResourceLoadWorker, pa::ResourceLoadWorker_Package and
//     pa::BasePackageResourceMap through MSVC RTTI in the running game image;
//   * patch slots of the Package worker's vtable so that a request for a file
//     listed in bin64\cdhd\manifest.tsv goes to the game's own loose-file
//     worker code, running on a fake worker whose resource map resolves the
//     game path to the file on disk;
//   * everything not in the manifest goes to the original pack reader.
//
// Manifest (UTF-8, TAB separated, '#' comments):
//   cdhd-runtime  1
//   build         <lowercase hex of meta/0.paver>  <CrimsonDesert.exe size in bytes>
//   file          <game path>  <absolute disk path>
//
// Flag files in bin64\cdhd\ : "verbose" logs every served file,
//                              "trace" logs every path the pack reader is asked for.

#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <unordered_map>
#include <atomic>
#include <mutex>

#define CDHD_VERSION "CDHD Runtime 1.0.0"
static const int kManifestVersion = 1;

// ---------------------------------------------------------------- globals used by hooks.asm
extern "C" {
uint64_t g_fakeWorker = 0;
uint64_t g_origPkg[16] = {};
uint64_t g_origBase[16] = {};
void CdhdHookSlot2();
void CdhdHookSlot3();
void CdhdHookSlot4();
void CdhdHookSlot5();
void CdhdHookSlot6();
void CdhdHookSlot12();
void CdhdHookSlot13();
}

// ---------------------------------------------------------------- logging
static std::mutex g_logMutex;
static FILE* g_log = nullptr;
static int g_logLines = 0;
static const int kMaxLogLines = 20000;
static bool g_verbose = false;
static bool g_trace = false;
static std::atomic<int> g_servedLogged{0};
static std::atomic<int> g_tracedLogged{0};
static std::atomic<int> g_status{0}; // 0 = starting, 1 = serving, -1 = stopped

static void Log(const char* fmt, ...)
{
    std::lock_guard<std::mutex> lock(g_logMutex);
    if (!g_log || g_logLines >= kMaxLogLines) return;
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(g_log, "[%02u:%02u:%02u.%03u] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
    va_list ap; va_start(ap, fmt); vfprintf(g_log, fmt, ap); va_end(ap);
    fputc('\n', g_log);
    if (++g_logLines == kMaxLogLines) fputs("[log] line limit reached, logging stops here\n", g_log);
    fflush(g_log);
}

// ---------------------------------------------------------------- served file table
// The game passes a path as P where **P is the char* of the path string.
// Resolved disk paths are handed back as a game-style string object.
#pragma pack(push, 1)
struct GameString {
    const char* data;
    uint32_t length;
    uint32_t hash;   // 0xFFFFFFFF = not computed
    uint32_t flags;  // 0x80000000 as the engine uses for static strings
    uint32_t pad;
    char text[1];
};
#pragma pack(pop)

struct ServedFile {
    GameString* diskPath = nullptr;
    uint64_t size = 0;
};

static std::unordered_map<std::string, ServedFile> g_files; // read-only once hooks are live

static std::string NormalizeKey(const char* s)
{
    std::string out;
    if (!s) return out;
    while (*s == '/' || *s == '\\') ++s;
    for (; *s; ++s) {
        char c = *s;
        if (c == '\\') c = '/';
        else if (c >= 'A' && c <= 'Z') c = (char)(c + 32);
        out.push_back(c);
    }
    return out;
}

static const char* PathText(void* p)
{
    if (!p) return nullptr;
    void* a = *(void**)p;
    if (!a) return nullptr;
    return *(const char**)a;
}

static const ServedFile* Lookup(void* path)
{
    if (g_files.empty()) return nullptr;
    const char* s = PathText(path);
    if (!s || !*s) return nullptr;
    auto it = g_files.find(NormalizeKey(s));
    return it == g_files.end() ? nullptr : &it->second;
}

extern "C" int CdhdShouldServe(void* path, int slot)
{
    const ServedFile* f = Lookup(path);
    if (g_trace && g_tracedLogged.fetch_add(1) < 5000) {
        const char* s = PathText(path);
        Log("trace slot%d %s%s", slot, s ? s : "(null)", f ? "  <- served" : "");
    }
    if (f && g_verbose && g_servedLogged.fetch_add(1) < 5000) {
        const char* s = PathText(path);
        Log("serve%s %s", slot == 13 ? " (async)" : "", s ? s : "?");
    }
    return f ? 1 : 0;
}

// ---------------------------------------------------------------- Package worker slots 10/11
typedef bool (*ExistsFn)(void*, void*);
typedef bool (*SizeFn)(void*, void*, uint64_t*);

static bool HookExists(void* self, void* path)
{
    if (Lookup(path)) return true;
    return ((ExistsFn)g_origPkg[10])(self, path);
}

static bool HookSize(void* self, void* path, uint64_t* out)
{
    if (const ServedFile* f = Lookup(path)) {
        if (out) *out = f->size;
        return true;
    }
    return ((SizeFn)g_origPkg[11])(self, path, out);
}

// ---------------------------------------------------------------- fake resource map
static void* MapRetThis(void* self) { return self; }
static uint64_t MapRetZero() { return 0; }
static void MapNop() {}
static uint64_t MapRetOne() { return 1; }
static bool MapExists(void*, void* path) { return Lookup(path) != nullptr; }
static bool MapSize(void*, void* path, uint64_t* out)
{
    const ServedFile* f = Lookup(path);
    if (!f) return false;
    if (out) *out = f->size;
    return true;
}
static bool MapNoIndex(void*, void*, int64_t* out)
{
    if (out) *out = -1;
    return false;
}
static bool MapResolve(void*, void* path, void** out)
{
    const ServedFile* f = Lookup(path);
    if (!f || !f->diskPath || !out) return false;
    *out = f->diskPath;
    return true;
}

// ---------------------------------------------------------------- RTTI search in the game image
struct Region { uint8_t* base; size_t size; };

static bool Readable(DWORD protect)
{
    if (protect & (PAGE_GUARD | PAGE_NOACCESS)) return false;
    DWORD p = protect & 0xFF;
    return p == PAGE_READONLY || p == PAGE_READWRITE || p == PAGE_WRITECOPY ||
           p == PAGE_EXECUTE_READ || p == PAGE_EXECUTE_READWRITE || p == PAGE_EXECUTE_WRITECOPY;
}

static std::vector<Region> ImageRegions(uint8_t* image)
{
    std::vector<Region> out;
    auto dos = (IMAGE_DOS_HEADER*)image;
    auto nt = (IMAGE_NT_HEADERS64*)(image + dos->e_lfanew);
    uint8_t* end = image + nt->OptionalHeader.SizeOfImage;
    uint8_t* p = image;
    MEMORY_BASIC_INFORMATION mbi;
    while (p < end && VirtualQuery(p, &mbi, sizeof(mbi)) == sizeof(mbi)) {
        uint8_t* rb = (uint8_t*)mbi.BaseAddress;
        uint8_t* re = rb + mbi.RegionSize;
        if (re > end) re = end;
        if (mbi.State == MEM_COMMIT && Readable(mbi.Protect) && re > p) out.push_back({ p, (size_t)(re - p) });
        p = re;
    }
    return out;
}

// SEH-guarded scanners (no C++ objects with destructors inside).
static uint8_t* FindBytes(const Region* regions, size_t count, const char* needle, size_t len, uint8_t* after)
{
    for (size_t r = 0; r < count; ++r) {
        __try {
            uint8_t* b = regions[r].base;
            size_t n = regions[r].size;
            if (n < len || b + n <= after) continue;
            size_t start = after >= b ? (size_t)(after - b) + 1 : 0;
            for (size_t i = start; i + len <= n; ) {
                void* hit = memchr(b + i, needle[0], n - len + 1 - i);
                if (!hit) break;
                size_t at = (uint8_t*)hit - b;
                if (memcmp(b + at, needle, len) == 0) return b + at;
                i = at + 1;
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    return nullptr;
}

static uint8_t* FindCompleteObjectLocator(const Region* regions, size_t count, uint8_t* image, uint32_t typeRva)
{
    for (size_t r = 0; r < count; ++r) {
        __try {
            uint8_t* b = regions[r].base;
            size_t n = regions[r].size & ~(size_t)3;
            for (size_t i = 12; i + 12 <= n; i += 4) {
                if (*(uint32_t*)(b + i) != typeRva) continue;
                uint8_t* col = b + i - 12;
                uint32_t signature = *(uint32_t*)col;
                uint32_t offset = *(uint32_t*)(col + 4);
                uint32_t selfRva = *(uint32_t*)(col + 20);
                if (signature == 1 && offset == 0 && selfRva == (uint32_t)(col - image)) return col;
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    return nullptr;
}

static uint64_t* FindVtable(const Region* regions, size_t count, uint8_t* col)
{
    for (size_t r = 0; r < count; ++r) {
        __try {
            uint8_t* b = regions[r].base;
            size_t n = regions[r].size & ~(size_t)7;
            for (size_t i = 0; i + 16 <= n; i += 8)
                if (*(uint64_t*)(b + i) == (uint64_t)col) return (uint64_t*)(b + i + 8);
        } __except (EXCEPTION_EXECUTE_HANDLER) {}
    }
    return nullptr;
}

struct ClassInfo { uint64_t* vtable = nullptr; uint8_t* col = nullptr; };

static ClassInfo FindClass(const std::vector<Region>& regions, uint8_t* image, const char* mangled)
{
    ClassInfo info;
    std::string needle(mangled);
    needle.push_back('\0');
    uint8_t* name = nullptr;
    for (int tries = 0; tries < 16; ++tries) {
        name = FindBytes(regions.data(), regions.size(), needle.c_str(), needle.size(), name);
        if (!name) break;
        uint32_t typeRva = (uint32_t)(name - 0x10 - image);
        info.col = FindCompleteObjectLocator(regions.data(), regions.size(), image, typeRva);
        if (info.col) {
            info.vtable = FindVtable(regions.data(), regions.size(), info.col);
            return info;
        }
    }
    return info;
}

// ---------------------------------------------------------------- paths & manifest
static std::wstring g_exeDir;   // ...\bin64
static std::wstring g_dataDir;  // ...\bin64\cdhd

static std::wstring ParentDir(const std::wstring& p)
{
    size_t k = p.find_last_of(L"\\/");
    return k == std::wstring::npos ? p : p.substr(0, k);
}

static bool ReadWholeFile(const std::wstring& path, std::string& out)
{
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER sz{};
    bool ok = GetFileSizeEx(h, &sz) && sz.QuadPart < (64LL << 20);
    if (ok) {
        out.resize((size_t)sz.QuadPart);
        DWORD got = 0;
        ok = sz.QuadPart == 0 || (ReadFile(h, &out[0], (DWORD)sz.QuadPart, &got, nullptr) && got == (DWORD)sz.QuadPart);
    }
    CloseHandle(h);
    return ok;
}

static std::wstring Utf8ToWide(const std::string& s)
{
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &w[0], n);
    return w;
}

static std::string WideToUtf8(const std::wstring& w)
{
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.data(), (int)w.size(), &s[0], n, nullptr, nullptr);
    return s;
}

static GameString* MakeGameString(const std::string& s)
{
    auto g = (GameString*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(GameString) + s.size() + 1);
    if (!g) return nullptr;
    memcpy(g->text, s.data(), s.size());
    g->text[s.size()] = 0;
    g->data = g->text;
    g->length = (uint32_t)s.size();
    g->hash = 0xFFFFFFFF;
    g->flags = 0x80000000;
    return g;
}

static std::vector<std::string> SplitTabs(const std::string& line)
{
    std::vector<std::string> f;
    size_t s = 0;
    for (;;) {
        size_t t = line.find('\t', s);
        f.push_back(line.substr(s, t == std::string::npos ? std::string::npos : t - s));
        if (t == std::string::npos) break;
        s = t + 1;
    }
    return f;
}

static bool LoadManifest()
{
    std::string text;
    if (!ReadWholeFile(g_dataDir + L"\\manifest.tsv", text)) {
        Log("no manifest (bin64\\cdhd\\manifest.tsv): nothing is served, the game runs as it ships");
        return false;
    }

    std::string paver;
    std::wstring gameDir = ParentDir(g_exeDir);
    if (!ReadWholeFile(gameDir + L"\\meta\\0.paver", paver)) {
        Log("STOP: could not read meta\\0.paver; nothing is served");
        return false;
    }
    static const char* hex = "0123456789abcdef";
    std::string paverHex;
    for (unsigned char c : paver) { paverHex.push_back(hex[c >> 4]); paverHex.push_back(hex[c & 15]); }

    wchar_t exePath[MAX_PATH * 2] = {};
    GetModuleFileNameW(nullptr, exePath, (DWORD)(sizeof(exePath) / sizeof(exePath[0])));
    WIN32_FILE_ATTRIBUTE_DATA exeAttr{};
    if (!GetFileAttributesExW(exePath, GetFileExInfoStandard, &exeAttr)) {
        Log("STOP: could not read the game exe size; nothing is served");
        return false;
    }
    uint64_t exeSize = ((uint64_t)exeAttr.nFileSizeHigh << 32) | exeAttr.nFileSizeLow;

    bool haveVersion = false, haveBuild = false;
    int lineNo = 0, missing = 0;
    size_t pos = 0;
    while (pos < text.size()) {
        size_t nl = text.find('\n', pos);
        std::string line = text.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        pos = nl == std::string::npos ? text.size() : nl + 1;
        ++lineNo;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (lineNo == 1 && line.size() >= 3 && (uint8_t)line[0] == 0xEF && (uint8_t)line[1] == 0xBB && (uint8_t)line[2] == 0xBF) line.erase(0, 3);
        if (line.empty() || line[0] == '#') continue;
        auto f = SplitTabs(line);
        if (!haveVersion) {
            if (f.size() < 2 || f[0] != "cdhd-runtime" || atoi(f[1].c_str()) != kManifestVersion) {
                Log("STOP: manifest line %d is not a version %d manifest; nothing is served (update the builder or this plugin)", lineNo, kManifestVersion);
                return false;
            }
            haveVersion = true;
            continue;
        }
        if (f[0] == "build" && f.size() >= 3) {
            uint64_t wantSize = _strtoui64(f[2].c_str(), nullptr, 10);
            if (f[1] != paverHex || wantSize != exeSize) {
                Log("STOP: the textures were installed for game build %s / exe %llu bytes, this is %s / %llu bytes. "
                    "The game has updated: nothing is served. Run the builder again.",
                    f[1].c_str(), (unsigned long long)wantSize, paverHex.c_str(), (unsigned long long)exeSize);
                return false;
            }
            haveBuild = true;
            continue;
        }
        if (f[0] == "file" && f.size() >= 3) {
            if (!haveBuild) {
                Log("STOP: manifest line %d lists a file before the build line; nothing is served", lineNo);
                return false;
            }
            std::string key = NormalizeKey(f[1].c_str());
            WIN32_FILE_ATTRIBUTE_DATA a{};
            if (key.empty() || !GetFileAttributesExW(Utf8ToWide(f[2]).c_str(), GetFileExInfoStandard, &a) ||
                (a.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
                if (++missing <= 50) Log("missing %s <- %s: the game's own file is used", f[2].c_str(), key.c_str());
                continue;
            }
            ServedFile sf;
            sf.diskPath = MakeGameString(f[2]);
            sf.size = ((uint64_t)a.nFileSizeHigh << 32) | a.nFileSizeLow;
            if (sf.diskPath) g_files[key] = sf;
            continue;
        }
    }
    if (!haveBuild) {
        Log("STOP: the manifest has no build line; nothing is served");
        g_files.clear();
        return false;
    }
    Log("%zu file(s) to serve%s%s", g_files.size(), missing ? ", some missing (see above)" : "", g_verbose ? " (verbose)" : "");
    return !g_files.empty();
}

// ---------------------------------------------------------------- patching
static bool WriteSlot(uint64_t* slot, uint64_t value)
{
    DWORD old = 0;
    if (!VirtualProtect(slot, 8, PAGE_READWRITE, &old)) return false;
    *slot = value;
    DWORD tmp = 0;
    VirtualProtect(slot, 8, old, &tmp);
    FlushInstructionCache(GetCurrentProcess(), slot, 8);
    return true;
}

static uint64_t* MakeFakeVtable(uint8_t* col, const uint64_t* entries, int count)
{
    auto block = (uint64_t*)VirtualAlloc(nullptr, 0x1000, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!block) return nullptr;
    block[0] = (uint64_t)col;           // RTTI locator at vtable[-1]
    memcpy(block + 1, entries, count * sizeof(uint64_t));
    return block + 1;
}

static bool InstallHooks()
{
    uint8_t* image = (uint8_t*)GetModuleHandleW(nullptr);
    ClassInfo base, pkg, map;
    DWORD t0 = GetTickCount();
    for (int attempt = 0; attempt < 60; ++attempt) {
        auto regions = ImageRegions(image);
        base = FindClass(regions, image, ".?AVBaseResourceLoadWorker@pa@@");
        pkg = FindClass(regions, image, ".?AVResourceLoadWorker_Package@pa@@");
        map = FindClass(regions, image, ".?AVBasePackageResourceMap@pa@@");
        if (base.vtable && pkg.vtable && map.col) break;
        Sleep(1000);
    }
    if (!base.vtable || !pkg.vtable || !map.col) {
        Log("STOP: the game's resource loader classes were not found (base=%p package=%p map=%p); nothing is served",
            base.vtable, pkg.vtable, map.col);
        return false;
    }
    Log("classes found in %lu ms (base vtable +0x%llx, package vtable +0x%llx)", GetTickCount() - t0,
        (unsigned long long)((uint8_t*)base.vtable - image), (unsigned long long)((uint8_t*)pkg.vtable - image));

    for (int i = 0; i < 14; ++i) { g_origBase[i] = base.vtable[i]; g_origPkg[i] = pkg.vtable[i]; }

    // Fake resource map: answers "where is this file" for served paths.
    uint64_t mapEntries[11] = {
        (uint64_t)&MapRetThis, (uint64_t)&MapRetZero, (uint64_t)&MapRetZero, (uint64_t)&MapNop,
        (uint64_t)&MapExists, (uint64_t)&MapSize, (uint64_t)&MapNoIndex, (uint64_t)&MapResolve,
        (uint64_t)&MapRetOne, (uint64_t)&MapRetZero, (uint64_t)&MapRetZero };
    uint64_t* mapVtbl = MakeFakeVtable(map.col, mapEntries, 11);

    // Fake worker: the game's loose-file worker code, with our map attached.
    uint64_t workerEntries[14];
    memcpy(workerEntries, base.vtable, sizeof(workerEntries));
    workerEntries[0] = (uint64_t)&MapRetThis;
    workerEntries[1] = (uint64_t)&MapRetZero;
    uint64_t* workerVtbl = MakeFakeVtable(base.col, workerEntries, 14);

    auto objects = (uint8_t*)VirtualAlloc(nullptr, 0x1000, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!mapVtbl || !workerVtbl || !objects) { Log("STOP: out of memory"); return false; }
    uint8_t* fakeWorker = objects;
    uint8_t* fakeMap = objects + 0x200;
    *(uint64_t**)fakeMap = mapVtbl;
    *(uint64_t**)fakeWorker = workerVtbl;
    *(uint32_t*)(fakeWorker + 0x10) = 0x10000000;
    fakeWorker[0x14] = 1;
    fakeWorker[0x16] = 1;
    *(uint8_t**)(fakeWorker + 0x18) = fakeMap;
    g_fakeWorker = (uint64_t)fakeWorker;

    struct { int slot; void* hook; } patches[] = {
        { 3, (void*)&CdhdHookSlot3 }, { 4, (void*)&CdhdHookSlot4 }, { 5, (void*)&CdhdHookSlot5 },
        { 6, (void*)&CdhdHookSlot6 }, { 12, (void*)&CdhdHookSlot12 },
        { 11, (void*)&HookSize }, { 10, (void*)&HookExists }, { 13, (void*)&CdhdHookSlot13 },
        { 2, (void*)&CdhdHookSlot2 },   // last: from here on requests are served
    };
    for (auto& p : patches) {
        if (!WriteSlot(&pkg.vtable[p.slot], (uint64_t)p.hook)) {
            Log("STOP: pack reader slot %d could not be swapped", p.slot);
            return false;
        }
    }
    Log("serving: the game's pack reader asks the CDHD list first");
    return true;
}

// ---------------------------------------------------------------- entry
static DWORD WINAPI InitThread(void*)
{
    if (!LoadManifest()) { g_status = -1; return 0; }
    g_status = InstallHooks() ? 1 : -1;
    return 0;
}

extern "C" __declspec(dllexport) int CdhdRuntimeStatus() { return g_status.load(); }

static bool FileExistsW(const std::wstring& p)
{
    DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID)
{
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    DisableThreadLibraryCalls(module);

    wchar_t name[64];
    swprintf_s(name, L"Local\\CDHDRuntime.%lu", GetCurrentProcessId());
    HANDLE mtx = CreateMutexW(nullptr, TRUE, name);
    if (mtx && GetLastError() == ERROR_ALREADY_EXISTS) return TRUE; // another copy already runs

    wchar_t exePath[MAX_PATH * 2] = {};
    GetModuleFileNameW(nullptr, exePath, (DWORD)(sizeof(exePath) / sizeof(exePath[0])));
    g_exeDir = ParentDir(exePath);
    g_dataDir = g_exeDir + L"\\cdhd";
    CreateDirectoryW(g_dataDir.c_str(), nullptr);
    g_verbose = FileExistsW(g_dataDir + L"\\verbose");
    g_trace = FileExistsW(g_dataDir + L"\\trace");
    _wfopen_s(&g_log, (g_dataDir + L"\\CDHDRuntime.log").c_str(), L"w");

    wchar_t self[MAX_PATH * 2] = {};
    GetModuleFileNameW(module, self, (DWORD)(sizeof(self) / sizeof(self[0])));
    Log("%s loaded from %s", CDHD_VERSION, WideToUtf8(self).c_str());

    HANDLE t = CreateThread(nullptr, 0, InitThread, nullptr, 0, nullptr);
    if (t) CloseHandle(t);
    return TRUE;
}
