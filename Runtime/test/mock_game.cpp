// Mock "game" for testing CDHDRuntime.asi without Crimson Desert.
// It declares classes with the same RTTI names and the same vtable slot roles
// the plugin relies on, loads the plugin, and checks that served paths are read
// from disk through the loose-file worker while other paths stay in the "pack".
//
// Usage: mock_game.exe <path to CDHDRuntime.asi>
//        (expects ..\meta\0.paver and cdhd\manifest.tsv next to the exe)

#include <windows.h>
#include <cstdio>
#include <cstdint>
#include <string>

struct GameStr { const char* data; uint32_t len; uint32_t hash; uint32_t flags; };

struct Request {
    uint64_t r0 = 0, r1 = 0, r2 = 0, r3 = 0;
    void* owner = nullptr; // +0x20: worker that created the request
    std::string data;
};

namespace pa {

class BasePackageResourceMap {
public:
    virtual ~BasePackageResourceMap() {}
    virtual uint64_t m1() { return 0; }
    virtual uint64_t m2() { return 0; }
    virtual void m3() {}
    virtual bool Exists(void*) { return false; }
    virtual bool Size(void*, uint64_t*) { return false; }
    virtual bool Index(void*, int64_t*) { return false; }
    virtual bool Resolve(void*, void**) { return false; }
    virtual uint64_t m8() { return 1; }
    virtual uint64_t m9() { return 0; }
    virtual uint64_t m10() { return 0; }
};

class BaseResourceLoadWorker {
public:
    uint64_t pad8 = 0;
    uint32_t flags = 0;          // +0x10
    uint8_t b14 = 0, b15 = 0, b16 = 0, b17 = 0;
    BasePackageResourceMap* map = nullptr; // +0x18

    virtual ~BaseResourceLoadWorker() {}
    virtual uint64_t s1() { return 0; }
    // slot 2: synchronous load -> returns a Request*
    virtual uint64_t Load(void* ctx, void* path, int mode)
    {
        void* obj = nullptr;
        if (!map || !map->Resolve(path, &obj) || !obj) return 0;
        const char* disk = ((GameStr*)obj)->data;
        auto r = new Request();
        r->owner = this;
        FILE* f = nullptr;
        if (fopen_s(&f, disk, "rb") == 0 && f) {
            char buf[4096]; size_t n;
            while ((n = fread(buf, 1, sizeof(buf), f)) > 0) r->data.append(buf, n);
            fclose(f);
        }
        return (uint64_t)r;
    }
    virtual uint64_t Op3(void* req, uint64_t a, uint64_t b, uint64_t c, uint64_t d, uint64_t e) { return 1000 + ((Request*)req)->data.size() + a + b + c + d + e; }
    virtual uint64_t Op4(void* req) { return 1004; }
    virtual uint64_t Op5(void* req) { return 1005; }
    virtual uint64_t Op6(void* req) { return 1006; }
    virtual uint64_t s7() { return 7; }
    virtual uint64_t s8() { return 8; }
    virtual uint64_t s9() { return 9; }
    virtual bool Exists(void* path) { return false; }
    virtual bool Size(void* path, uint64_t* out) { return false; }
    virtual uint64_t Op12(void* ctx, void** reqRef) { return 1012; }
    virtual uint64_t LoadAsync(void* ctx, void* path, void* cb) { return Load(ctx, path, 0) ? 1013 : 0; }
};

class ResourceLoadWorker_Package : public BaseResourceLoadWorker {
public:
    uint64_t Load(void* ctx, void* path, int mode) override
    {
        auto r = new Request();
        r->owner = this;
        r->data = std::string("PAZ:") + **(const char***)path;
        return (uint64_t)r;
    }
    uint64_t Op3(void* req, uint64_t a, uint64_t b, uint64_t c, uint64_t d, uint64_t e) override { return 2000 + ((Request*)req)->data.size() + a + b + c + d + e; }
    uint64_t Op4(void* req) override { return 2004; }
    uint64_t Op5(void* req) override { return 2005; }
    uint64_t Op6(void* req) override { return 2006; }
    bool Exists(void* path) override { return std::string(**(const char***)path) == "packed/only.dds"; }
    bool Size(void* path, uint64_t* out) override { if (out) *out = 77; return true; }
    uint64_t Op12(void* ctx, void** reqRef) override { return 2012; }
    uint64_t LoadAsync(void* ctx, void* path, void* cb) override { return 2013; }
};

} // namespace pa

__declspec(noinline) pa::BaseResourceLoadWorker* MakeWorker()
{
    static pa::BasePackageResourceMap realMap; // keeps the map class (and its RTTI) in the image
    volatile auto* w = new pa::ResourceLoadWorker_Package();
    w->map = &realMap;
    return (pa::BaseResourceLoadWorker*)w;
}

static int g_fail = 0;
static void Check(bool ok, const char* what) { printf("%s  %s\n", ok ? "PASS" : "FAIL", what); if (!ok) ++g_fail; }

int main(int argc, char** argv)
{
    if (argc < 2) { printf("usage: mock_game <CDHDRuntime.asi>\n"); return 2; }
    pa::BaseResourceLoadWorker* w = MakeWorker();

    HMODULE m = LoadLibraryA(argv[1]);
    if (!m) { printf("FAIL  cannot load %s (%lu)\n", argv[1], GetLastError()); return 1; }
    auto status = (int (*)())GetProcAddress(m, "CdhdRuntimeStatus");
    int st = 0;
    for (int i = 0; i < 300 && status && (st = status()) == 0; ++i) Sleep(100);
    Check(st == 1, "plugin reports serving");
    if (st != 1) return 1;

    GameStr served{ "/Tree/Texture/Test_Color.dds", 0, 0, 0 };
    GameStr* sp = &served;
    GameStr packed{ "packed/only.dds", 0, 0, 0 };
    GameStr* pp = &packed;

    auto r1 = (Request*)w->Load(nullptr, &sp, 0);
    Check(r1 && r1->data == "HELLO-HD-TEXTURE", "served path is read from disk (case/slash-insensitive)");
    Check(r1 && r1->owner != w, "served request belongs to the fake worker");
    Check(r1 && w->Op3(r1, 1, 2, 3, 4, 5) == 1000 + 16 + 15, "slot 3 on a served request goes to the loose worker (stack args intact)");
    Check(r1 && w->Op4(r1) == 1004 && w->Op5(r1) == 1005 && w->Op6(r1) == 1006, "slots 4-6 on a served request go to the loose worker");
    void* ref = r1;
    Check(w->Op12(nullptr, &ref) == 1012, "slot 12 on a served request goes to the loose worker");

    auto r2 = (Request*)w->Load(nullptr, &pp, 0);
    Check(r2 && r2->data == "PAZ:packed/only.dds", "unlisted path stays in the pack");
    Check(r2 && w->Op3(r2, 1, 2, 3, 4, 5) == 2000 + 19 + 15, "slot 3 on a pack request stays in the pack");
    void* ref2 = r2;
    Check(w->Op12(nullptr, &ref2) == 2012, "slot 12 on a pack request stays in the pack");

    uint64_t size = 0;
    Check(w->Exists(&sp) && w->Size(&sp, &size) && size == 16, "exists/size answer for a served file");
    Check(w->Exists(&pp) && w->Size(&pp, &size) && size == 77, "exists/size fall through for a pack file");
    Check(w->LoadAsync(nullptr, &sp, nullptr) == 1013, "async load of a served path uses the loose worker");
    Check(w->LoadAsync(nullptr, &pp, nullptr) == 2013, "async load of a pack path stays in the pack");

    printf(g_fail ? "\n%d check(s) FAILED\n" : "\nall checks passed\n", g_fail);
    return g_fail ? 1 : 0;
}
