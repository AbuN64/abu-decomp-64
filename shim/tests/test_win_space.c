/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The guest's placeholder ranges (os/win_space.c) over a model of the Win32 rules the Windows backend relies on (measured on
 * Windows 10 19045): a placeholder is replaced by a view or a private reservation only whole, an allocation freed with
 * MEM_PRESERVE_PLACEHOLDER becomes a placeholder again, adjacent placeholders coalesce only over their exact range, and a
 * plain reservation cannot be made over a placeholder. No Windows installation needed. */
#include "../os/win_space.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int checks;
#define CHECK(value) do { checks++; if (!(value)) { fprintf(stderr,"FAIL win_space:%d: %s\n",__LINE__,#value); exit(1); } } while (0)
enum { G=N64C_SPACE_GRANULE, FREE_STATE=0x10000, RESERVE_STATE=0x2000, COMMIT_STATE=0x1000, PRIVATE_TYPE=0x20000, IMAGE_TYPE=0x1000000 };
enum { PLACEHOLDER, VIEW, PRIVATE, FOREIGN };
typedef struct { uintptr_t base; size_t size; int kind; uint32_t physical; char module[32]; } Region;
static Region regions[256];
static unsigned region_count;
static unsigned calls_reserve, calls_release, calls_coalesce, calls_map, calls_unmap, calls_alloc;
static int fail_map, fail_coalesce_at;   /* fail the map call; fail the nth coalesce call (1-based) */
static char logbuf[8192];

static Region *find(uintptr_t address)
{
    for (unsigned i=0;i<region_count;i++)
        if (address>=regions[i].base && address<regions[i].base+regions[i].size) return &regions[i];
    return NULL;
}
static void insert(Region region)
{
    unsigned at=0;
    while (at<region_count && regions[at].base<region.base) at++;
    memmove(&regions[at+1],&regions[at],(region_count-at)*sizeof regions[0]);
    regions[at]=region;
    region_count++;
}
static void erase(Region *region)
{
    memmove(region,region+1,(size_t)(&regions[region_count]-region-1)*sizeof regions[0]);
    region_count--;
}
static void reset(void)
{
    region_count=0;
    calls_reserve=calls_release=calls_coalesce=calls_map=calls_unmap=calls_alloc=0;
    fail_map=fail_coalesce_at=0;
    logbuf[0]=0;
}
static int m_reserve(void *context,uintptr_t address,size_t bytes)
{
    (void)context;calls_reserve++;
    for (unsigned i=0;i<region_count;i++)
        if (regions[i].base<address+bytes && address<regions[i].base+regions[i].size) return 0;
    insert((Region){address,bytes,PLACEHOLDER,0,""});
    return 1;
}
static int m_release(void *context,uintptr_t address,size_t bytes)
{
    (void)context;calls_release++;
    Region *r=find(address);
    if (!r || address+bytes>r->base+r->size) return 0;
    if (r->kind!=PLACEHOLDER) {
        if (r->base!=address || r->size!=bytes) return 0;
        r->kind=PLACEHOLDER;
        return 1;
    }
    if (r->base==address && r->size==bytes) return 0;   /* a whole placeholder is not split */
    const Region whole=*r;
    erase(r);
    if (whole.base<address) insert((Region){whole.base,address-whole.base,PLACEHOLDER,0,""});
    insert((Region){address,bytes,PLACEHOLDER,0,""});
    if (address+bytes<whole.base+whole.size) insert((Region){address+bytes,whole.base+whole.size-address-bytes,PLACEHOLDER,0,""});
    return 1;
}
static int m_coalesce(void *context,uintptr_t address,size_t bytes)
{
    (void)context;calls_coalesce++;
    if (fail_coalesce_at==(int)calls_coalesce) return 0;
    Region *first=find(address);
    if (!first || first->base!=address || first->kind!=PLACEHOLDER) return 0;
    size_t have=first->size;
    unsigned merged=0;
    while (have<bytes) {
        Region *next=find(address+have);
        if (!next || next->base!=address+have || next->kind!=PLACEHOLDER) return 0;
        have+=next->size;
        merged++;
    }
    if (have!=bytes || !merged) return 0;
    for (unsigned i=0;i<merged;i++) erase(first+1);
    first->size=bytes;
    return 1;
}
static int m_map(void *context,uintptr_t address,size_t bytes,uint32_t physical)
{
    (void)context;calls_map++;
    Region *r=find(address);
    if (fail_map || !r || r->base!=address || r->size!=bytes || r->kind!=PLACEHOLDER) return 0;
    r->kind=VIEW;
    r->physical=physical;
    return 1;
}
static int m_unmap(void *context,uintptr_t address,size_t bytes)
{
    (void)context;(void)bytes;calls_unmap++;
    Region *r=find(address);
    if (!r || r->base!=address || r->kind!=VIEW) return 0;
    r->kind=PLACEHOLDER;
    return 1;
}
static int m_alloc(void *context,uintptr_t address,size_t bytes)
{
    (void)context;calls_alloc++;
    Region *r=find(address);
    if (!r || r->base!=address || r->size!=bytes || r->kind!=PLACEHOLDER) return 0;   /* VirtualAlloc over a placeholder fails too */
    r->kind=PRIVATE;
    return 1;
}
static int m_query(void *context,uintptr_t address,N64cSpaceRegion *out)
{
    (void)context;
    const Region *r=find(address);
    memset(out,0,sizeof *out);
    if (!r) {
        uintptr_t end=address+G;
        for (unsigned i=0;i<region_count;i++)
            if (regions[i].base>address && regions[i].base<end) end=regions[i].base;
        out->base=address;out->allocation_base=0;out->size=end-address;out->state=FREE_STATE;
        return 1;
    }
    out->base=out->allocation_base=r->base;
    out->size=r->size;
    out->state=r->kind==VIEW||r->kind==FOREIGN ? COMMIT_STATE : RESERVE_STATE;
    out->type=r->kind==FOREIGN ? IMAGE_TYPE : r->kind==VIEW ? 0x40000 : PRIVATE_TYPE;
    strcpy(out->module,r->module);
    return 1;
}
static void m_log(void *context,const char *line)
{
    (void)context;
    strncat(logbuf,line,sizeof logbuf-strlen(logbuf)-2);
    strcat(logbuf,"\n");
}
static const N64cSpaceOps ops={NULL,m_reserve,m_release,m_coalesce,m_map,m_unmap,m_alloc,m_query,m_log};

static unsigned count(int kind)
{
    unsigned n=0;
    for (unsigned i=0;i<region_count;i++) n+=regions[i].kind==kind;
    return n;
}
static void fresh(N64cSpace *space)
{
    reset();
    CHECK(n64c_space_reserve(&ops)==0);
    CHECK(n64c_space_adopt(space,&ops)==0);
}

int main(void)
{
    N64cSpace space;
    /* the zones become 48 placeholders of one chunk before anything else runs, and the process the loader runs adopts them */
    fresh(&space);
    CHECK(region_count==N64C_SPACE_CHUNKS && count(PLACEHOLDER)==N64C_SPACE_CHUNKS && calls_reserve==N64C_SPACE_CHUNKS);
    CHECK(find(0x00400000)->size==G && find(0x00800000)->size==G && find(0x009F0000)->size==G && !find(0x00500000) && !find(0x00A00000));

    /* the log's failing page: a 4 KiB page of osMapTLB 0x0015c000 -> 0x0044b000 needs its 64 KiB block, which is a placeholder
     * the guest zone holds; the block replaces it, is there for every later page, and gives the chunk back when it ends */
    CHECK(n64c_space_block(&space,0x0044b000)==1);
    CHECK(find(0x00440000)->kind==PRIVATE && find(0x00440000)->size==G && calls_alloc==1);
    CHECK(n64c_space_block(&space,0x0044c000)==1 && calls_alloc==1);
    CHECK(n64c_space_block_end(&space,0x0044b000)==1 && find(0x00440000)->kind==PLACEHOLDER);
    CHECK(n64c_space_block_end(&space,0x0044b000)==0);

    /* a 1 MB view at 0x00400000: the chunks coalesce, the view replaces the placeholder in one call, and ending it restores
     * the chunks, so the range is never unowned */
    calls_coalesce=calls_map=calls_release=0;
    CHECK(n64c_space_view(&space,0x00400000,0x100000,0)==1);
    CHECK(calls_coalesce==15 && calls_map==1 && find(0x00400000)->kind==VIEW && find(0x00400000)->size==0x100000);
    CHECK(find(0x00400000)->physical==0 && region_count==N64C_SPACE_CHUNKS-15);
    CHECK(n64c_space_block(&space,0x0044b000)==-1);   /* the range is a view now */
    CHECK(n64c_space_view_end(&space,0x00400000,0x100000)==1);
    CHECK(calls_unmap==1 && calls_release==15 && count(PLACEHOLDER)==N64C_SPACE_CHUNKS && find(0x0040f000)->size==G);
    CHECK(n64c_space_view_end(&space,0x00400000,0x100000)==0);
    CHECK(n64c_space_block(&space,0x0044b000)==1);    /* and a block can take a chunk of it again */
    CHECK(n64c_space_block_end(&space,0x0044b000)==1);

    /* a view over a chunk that is a block is refused with the occupant named, and nothing changes */
    CHECK(n64c_space_block(&space,0x00810000)==1);
    logbuf[0]=0;calls_coalesce=calls_map=0;
    CHECK(n64c_space_view(&space,0x00800000,0x20000,0x20000)==-1);
    CHECK(calls_coalesce==0 && calls_map==0 && find(0x00810000)->kind==PRIVATE && find(0x00800000)->size==G);
    CHECK(strstr(logbuf,"base 0x810000 size 0x10000 state 0x2000 type 0x20000 allocation base 0x810000"));
    CHECK(n64c_space_block_end(&space,0x00810000)==1);

    /* a map that fails leaves the chunks as placeholders and the range usable */
    fail_map=1;
    CHECK(n64c_space_view(&space,0x00900000,0x40000,0)==-1);
    CHECK(count(PLACEHOLDER)==N64C_SPACE_CHUNKS && find(0x00900000)->size==G && find(0x00930000)->size==G);
    CHECK(strstr(logbuf,"cannot map the guest range"));
    fail_map=0;
    CHECK(n64c_space_view(&space,0x00900000,0x40000,0x40000)==1 && find(0x00900000)->physical==0x40000);
    CHECK(n64c_space_view_end(&space,0x00900000,0x40000)==1 && count(PLACEHOLDER)==N64C_SPACE_CHUNKS);
    /* so does a coalesce that fails half way */
    calls_coalesce=0;fail_coalesce_at=3;
    CHECK(n64c_space_view(&space,0x00900000,0x80000,0)==-1);
    CHECK(count(PLACEHOLDER)==N64C_SPACE_CHUNKS && find(0x00900000)->size==G && find(0x00910000)->size==G);
    fail_coalesce_at=0;

    /* outside the zones nothing is held: the caller maps directly, and a range that is only partly held is refused */
    calls_coalesce=calls_map=calls_alloc=0;
    CHECK(n64c_space_view(&space,0x00500000,0x10000,0)==0 && n64c_space_view(&space,0x20000000,0x10000,0)==0);
    CHECK(n64c_space_block(&space,0x00600000)==0 && n64c_space_block_end(&space,0x00600000)==0);
    CHECK(n64c_space_view(&space,0x004F0000,0x20000,0)==-1);
    CHECK(calls_coalesce==0 && calls_map==0 && calls_alloc==0);

    /* the loader or a DLL took a chunk before the start reserved it: the reserve and the adoption name the occupant, and that
     * chunk alone is mapped directly while the others stay held */
    reset();
    insert((Region){0x00440000,G,FOREIGN,0,"opengl32.dll"});
    logbuf[0]=0;
    CHECK(n64c_space_reserve(&ops)==1);
    CHECK(strstr(logbuf,"cannot reserve the guest range 0x440000 +0x10000") && strstr(logbuf,"module opengl32.dll"));
    logbuf[0]=0;
    CHECK(n64c_space_adopt(&space,&ops)==1);
    CHECK(strstr(logbuf,"guest range not reserved at start") && strstr(logbuf,"state 0x1000 type 0x1000000"));
    CHECK(n64c_space_block(&space,0x0044b000)==0 && n64c_space_view(&space,0x00440000,0x10000,0)==0);
    CHECK(n64c_space_view(&space,0x00400000,0x40000,0)==1 && n64c_space_block(&space,0x00450000)==1);
    CHECK(n64c_space_view(&space,0x00430000,0x20000,0)==-1);   /* straddles the occupied chunk */

    /* the report walks every region of the range, free ones too */
    reset();
    insert((Region){0x00700000,G,FOREIGN,0,"mesa.dll"});
    logbuf[0]=0;
    n64c_space_report(&ops,0x006F0000,0x30000,"test");
    CHECK(strstr(logbuf,"test 0x6f0000 +0x30000"));
    CHECK(strstr(logbuf,"base 0x6f0000 size 0x10000 state 0x10000 type 0 allocation base 0 module (none)"));
    CHECK(strstr(logbuf,"base 0x700000 size 0x10000 state 0x1000 type 0x1000000 allocation base 0x700000 module mesa.dll"));
    CHECK(strstr(logbuf,"base 0x710000 size 0x10000 state 0x10000"));
    printf("win_space: %d checks passed\n",checks);
    return 0;
}
