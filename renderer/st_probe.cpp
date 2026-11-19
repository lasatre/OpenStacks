// st_probe: does renderer/third_party/shadertranslator/ShaderTranslator.h match the
// libshadertranslator.dll next to it? Compiles a highp fragment shader with
// FragmentPrecisionHigh = 1, exactly as gfxstream's ANGLE shader parser does.
//   st_probe.exe   -> prints the translator's verdict and the struct sizes we assume
#ifdef _WIN32
#include <windows.h>
#define ST_LIB "libshadertranslator.dll"
#define st_open(n) ((void *)LoadLibraryA(n))
#define st_sym(l, n) ((void *)GetProcAddress((HMODULE)(l), n))
#else
#include <dlfcn.h>
#define ST_LIB "./libshadertranslator.so"
#define st_open(n) dlopen(n, RTLD_NOW)
#define st_sym(l, n) dlsym(l, n)
#endif

#include <cstdint>
#include <cstdio>
#include <cstring>

#include "ShaderTranslator.h"

int main() {
    void* m = st_open(ST_LIB);
    if (!m) {
        printf("cannot load %s\n", ST_LIB);
        return 1;
    }
    auto init = (STInitialize_t)st_sym(m, "STInitialize");
    auto gen = (STGenerateResources_t)st_sym(m, "STGenerateResources");
    auto compile = (STCompileAndResolve_t)st_sym(m, "STCompileAndResolve");
    if (!init || !gen || !compile) {
        printf("missing exports\n");
        return 1;
    }
    printf("sizeof(ST_BuiltInResources)=%zu sizeof(ST_ShaderCompileResult)=%zu\n",
           sizeof(ST_BuiltInResources), sizeof(ST_ShaderCompileResult));
    init();
    static ST_BuiltInResources res;
    memset(&res, 0xAB, sizeof(res));   // marks fields the DLL does not write
    gen(&res);
    printf("after STGenerateResources: MaxVertexAttribs=%d FragmentPrecisionHigh=%d "
           "MaxDrawBuffers=%d\n", res.MaxVertexAttribs, res.FragmentPrecisionHigh,
           res.MaxDrawBuffers);
    {   // OST_DUMP: the DLL's view of the struct, as ints (0xABABABAB = not written)
        const int* w = (const int*)&res;
        for (unsigned i = 0; i < sizeof(res) / 4; i++) printf("%u:%d%s", i, w[i], i % 12 == 11 ? "\n" : " ");
        printf("\nour FragmentPrecisionHigh index = %zu\n",
               (size_t)(((const char*)&res.FragmentPrecisionHigh - (const char*)&res) / 4));
    }
    res.FragmentPrecisionHigh = 1;
    static const char kFrag[] =
        "\nprecision highp float;\nvarying highp vec2 vTexCoord;\n"
        "uniform sampler2D s;\nvoid main() { gl_FragColor = texture2D(s, vTexCoord); }\n";
    ST_ShaderCompileInfo ci = {nullptr, 0x8B30 /* GL_FRAGMENT_SHADER */, ST_GLES2_SPEC,
                               ST_GLSL_COMPATIBILITY_OUTPUT, ST_OBJECT_CODE | ST_VARIABLES,
                               &res, kFrag};
    ST_ShaderCompileResult* out = nullptr;
    compile(&ci, &out);
    if (!out) {
        printf("no result\n");
        return 1;
    }
    printf("compileStatus=%d\ninfoLog: %s\ntranslated:\n%s\n", (int)out->compileStatus,
           out->infoLog ? out->infoLog : "(null)",
           out->translatedSource ? out->translatedSource : "(null)");
    return 0;
}
