/* SPDX-License-Identifier: GPL-3.0-or-later */
/* n64c_pi: cartridge DMA served from the ROM file (.z64, big-endian).
 * After the copy the destination is fixed up to host byte order according to the resource type that owns the
 * ROM range. The descriptors are port data registered at start-up (n64c_pi_register), never generated code:
 *   RAW      bytes stay as in the ROM (code blobs, texels, TLUTs, audio sample data)
 *   SWAP16   array of 16-bit values
 *   SWAP32   array of 32-bit values (also display lists, which are 32-bit word pairs)
 *   STRUCT   repeated record described by a field string, e.g. "2222 4 11" (digits are field widths in bytes)
 *   TAGGED   records of different sizes laid back to back; a tag (1, 2 or 4 bytes, big-endian, at a fixed offset of
 *            each record) selects the record's field string and so its size (widget pages)
 *   IMAGE    data the source types: the range's bytes come from its native compile (overlay data units), not the ROM
 *   PACKED   one RNC stream: header fields in host order, packed bytes raw; its unpacked bytes are typed by a layout
 *            (any kind above, or GRAPH: a typed record graph of directories, leaves and records with offset
 *            references) applied after the game unpacks it
 *   UNTYPED  a blob of the decomp's blob facts that nothing types yet (the byte-order gate): a DMA that starts in it
 *            stops the boot naming the blob (N64C_LAYOUT_GATE=log: logs it once and serves it raw)
 * A DMA may cover any sub-range of a resource; alignment is taken relative to the resource start. Every DMA also
 * records where its bytes came from (RAM range -> cartridge offset), so an unpack call can name its stream. */
#include "n64c_internal.h"
#include "n64c_pi.h"
#include "n64c_al.h"
#include "n64c_rnc.h"
#include "n64c_diag.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { u32 tag, stride; char fields[64]; } TagLayout;
typedef struct { u32 offset, width, count; TagLayout layout[16]; } Tagged;
/* GRAPH types (n64c_pi.h): kind F/L/X/D/E/G/T/R/P; items: F widths (1-8) or references (REF | section << 16 | type),
 * L, X and E [type], D section types (NONE: untyped), G/T tag/type pairs; size: F record bytes; tag offset/width:
 * G/T. */
typedef struct { char kind; u32 size, offset, width, n; u32 *items; } GType;
typedef struct { u32 count; GType *types; } Graph;
#define GRAPH_REF 0x80000000u
#define GRAPH_NONE 0xFFFFFFFFu
/* How bytes are typed: kind, the field string of SWAP16/SWAP32/STRUCT records (stride bytes each), the tag layouts of
 * TAGGED, the program of GRAPH. */
typedef struct { N64cResKind kind; char fields[64]; u32 stride; const Tagged *tagged; Graph *graph; u32 root; } Layout;
#define RNC_HEADER 18u
#define MAX_RES 1024
typedef struct {
    u32 lo, hi; N64cResKind kind; char fields[64]; u32 stride; const Tagged *tagged; u8 *widths; const u8 *image;
    int logged; /* UNTYPED (or a PACKED stream whose unpacked kind is UNTYPED): fields is the blob name */
    u8 header[RNC_HEADER]; u32 unpacked_bytes; Layout unpacked; /* PACKED: host-order header, unpacked layout */
} Res;
static Res res[MAX_RES];
static int n_res;
/* RNC streams nested in a GRAPH-typed ROM range (P types): found when the range's graph is walked at registration;
 * their headers are typed by that range's width map, their unpacked bytes by the graph from the P type's target. */
static Res *nested;
static int n_nested, cap_nested;
static Tagged tagged_pool[8];
static int n_tagged;
typedef struct { uintptr_t lo, hi; u32 dev; } Origin; /* RAM [lo, hi) holds the cartridge bytes from dev */
#define MAX_ORIGIN 1024
static Origin origin[MAX_ORIGIN];
static int n_origin;
static pthread_mutex_t origin_lock=PTHREAD_MUTEX_INITIALIZER; /* DMAs and unpacks run on different game threads */
static FILE *rom;
static N64cRelocateFn relocate;
static void *relocate_user;

static void free_graph(Graph *graph)
{
    if (!graph) return;
    for (u32 t=0;t<graph->count;t++) free(graph->types[t].items);
    free(graph->types);
    free(graph);
}
void n64c_pi_set_relocator(N64cRelocateFn resolve,void *user)
{
    relocate=resolve;
    relocate_user=user;
}

int n64c_pi_open(const char *path)
{
    if (rom)
        fclose(rom);
    rom = path ? fopen(path, "rb") : NULL;
    return rom != NULL;
}
void n64c_pi_close(void)
{
    if (rom)
        fclose(rom);
    rom = NULL;
    free(nested);
    nested = NULL;
    n_nested = cap_nested = 0;
    for (int i = 0; i < n_res; i++) {
        free(res[i].widths);
        free_graph(res[i].unpacked.graph);
    }
    n_res = 0;
    n_tagged = 0;
    pthread_mutex_lock(&origin_lock);
    n_origin = 0;
    pthread_mutex_unlock(&origin_lock);
    n64c_pi_set_relocator(NULL,NULL);
}
static u32 field_width(char field)
{
    if (field=='p') return 4;
    return field>='1' && field<='8' ? (u32)(field-'0') : 0;
}
/* "offset/width;tag=fields;tag=fields..." (decimal numbers), each fields string as for STRUCT. */
static int parse_tagged(Tagged *out,const char *spec)
{
    char *end;
    unsigned long offset=strtoul(spec,&end,10);
    if (end==spec || *end!='/') return 0;
    spec=end+1;
    unsigned long width=strtoul(spec,&end,10);
    if (end==spec || (width!=1 && width!=2 && width!=4) || *end!=';') return 0;
    out->offset=(u32)offset;
    out->width=(u32)width;
    while (*end==';') {
        spec=end+1;
        unsigned long tag=strtoul(spec,&end,10);
        if (end==spec || *end!='=' || out->count>=16) return 0;
        TagLayout *layout=&out->layout[out->count];
        for (unsigned i=0;i<out->count;i++)
            if (out->layout[i].tag==tag) return 0;
        layout->tag=(u32)tag;
        unsigned count=0;
        for (spec=end+1;*spec && *spec!=';';spec++) {
            if (*spec==' ') continue;
            u32 w=field_width(*spec);
            if (!w || count>=sizeof layout->fields-1) return 0;
            layout->fields[count++]=*spec;
            layout->stride+=w;
        }
        if (!count || layout->stride<out->offset+out->width) return 0;
        out->count++;
        end=(char *)spec;
    }
    return *end==0 && out->count>0;
}
static int parse_u32(const char **text,u32 *value)
{
    char *end;
    if (**text<'0' || **text>'9') return 0;
    unsigned long parsed=strtoul(*text,&end,10);
    if (parsed>0xFFFFFFFFul) return 0;
    *value=(u32)parsed;
    *text=end;
    return 1;
}
/* One GRAPH type (n64c_pi.h) from text up to ';' or the end; references are checked once all types are known. */
static int parse_gtype(GType *type,const char **text)
{
    const char *p=*text;
    u32 cap=8;
    if (!p[0] || !strchr("FLXDEGTRP",p[0]) || p[1]!=':') return 0;
    type->kind=p[0];
    p+=2;
    if (type->kind=='G' || type->kind=='T') {
        if (!parse_u32(&p,&type->offset) || *p++!='/' || !parse_u32(&p,&type->width) || *p++!=':' ||
            (type->width!=1 && type->width!=2 && type->width!=4)) return 0;
    }
    type->items=malloc(cap*sizeof *type->items);
    if (!type->items) return 0;
    while (*p && *p!=';') {
        if (type->n+2>cap) {
            u32 *grown=realloc(type->items,(cap*=2)*sizeof *type->items);
            if (!grown) return 0;
            type->items=grown;
        }
        u32 a,b;
        if (type->kind=='F') {
            if (*p==' ') { p++; continue; }
            if (*p=='@') {
                p++;
                if (!parse_u32(&p,&a) || *p++!=':' || !parse_u32(&p,&b) || a>0x7FFF || b>0xFFFF) return 0;
                type->items[type->n++]=GRAPH_REF|a<<16|b;
                type->size+=4;
            } else {
                char c=*p++;
                u32 w=c=='p' ? 0 : field_width(c); /* no relocated pointers in a graph */
                if (!w) return 0;
                type->items[type->n++]=w;
                type->size+=w;
            }
        } else if (type->kind=='G' || type->kind=='T') {
            if (!parse_u32(&p,&a) || *p++!='=' || !parse_u32(&p,&b)) return 0;
            type->items[type->n++]=a;
            type->items[type->n++]=b;
            if (*p==',') p++;
        } else {
            if (*p=='-') { p++; a=GRAPH_NONE; }
            else if (!parse_u32(&p,&a)) return 0;
            type->items[type->n++]=a;
            if (*p==',') p++;
            else if (*p && *p!=';') return 0;
            if (((type->kind=='L' || type->kind=='X' || type->kind=='E' || type->kind=='P') && type->n>1) ||
                (type->kind=='R' && type->n>2))
                return 0;
        }
    }
    *text=p;
    return type->n>(type->kind=='R' ? 1u : 0u) && (type->kind!='F' || type->size>0);
}
static Graph *parse_graph(const char *text)
{
    Graph *graph=calloc(1,sizeof *graph);
    u32 cap=0;
    if (!graph || !text) { free(graph); return NULL; }
    int ok=1;
    for (const char *p=text;ok;) {
        if (graph->count==cap) {
            GType *grown=realloc(graph->types,(cap=cap ? cap*2 : 8)*sizeof *grown);
            if (!grown) { ok=0; break; }
            graph->types=grown;
        }
        GType *type=&graph->types[graph->count++];
        memset(type,0,sizeof *type);
        ok=parse_gtype(type,&p);
        if (!ok || !*p) break;
        p++; /* ';' */
    }
    /* every referenced type exists; a leaf holds F or T records, T variants, R parts and plane leaves F records (a
     * plane leaf's records hold no references: an offset cut into planes is not readable) */
    for (u32 t=0;ok && t<graph->count;t++) {
        GType *type=&graph->types[t];
        for (u32 i=0;ok && i<type->n;i++) {
            u32 item=type->items[i], target=type->kind=='F' ? (item&GRAPH_REF ? item&0xFFFF : 0) :
                type->kind=='G' || type->kind=='T' ? (i%2 ? item : 0) : item;
            if ((type->kind=='F' && !(item&GRAPH_REF)) || ((type->kind=='G' || type->kind=='T') && !(i%2))) continue;
            if (target==GRAPH_NONE) { ok=type->kind=='D' || type->kind=='P'; continue; } /* untyped: a section, or
                                                                                        a stream that unpacks raw */
            ok=target<graph->count;
            if (!ok) break;
            char kind=graph->types[target].kind;
            if (type->kind=='L') ok=kind=='F' || kind=='T';
            if (type->kind=='T' || type->kind=='R') ok=kind=='F';
            if (type->kind=='X') {
                const GType *record=&graph->types[target];
                ok=kind=='F';
                for (u32 k=0;ok && k<record->n;k++) ok=!(record->items[k]&GRAPH_REF);
            }
            if (type->kind=='T' && graph->types[target].size>type->size) type->size=graph->types[target].size;
        }
    }
    if (!ok) {
        free_graph(graph);
        return NULL;
    }
    return graph;
}
/* Parse kind + fields into layout (SWAP16, SWAP32, STRUCT, TAGGED, GRAPH, RAW); 0 on a malformed one. */
static int parse_layout(Layout *layout,N64cResKind kind,const char *fields)
{
    memset(layout,0,sizeof *layout);
    layout->kind=kind;
    if (kind==N64C_RES_GRAPH) {
        layout->graph=parse_graph(fields);
        layout->stride=1;
        return layout->graph!=NULL;
    }
    if (kind==N64C_RES_TAGGED) {
        if (!fields || n_tagged>=(int)(sizeof tagged_pool/sizeof tagged_pool[0])) return 0;
        Tagged *tagged=&tagged_pool[n_tagged];
        memset(tagged,0,sizeof *tagged);
        if (!parse_tagged(tagged,fields)) return 0;
        layout->tagged=tagged;
        layout->stride=1;
        n_tagged++;
    } else if (kind==N64C_RES_STRUCT) {
        unsigned count=0;
        if (!fields) return 0;
        for (const char *p=fields;*p;p++) {
            if (*p==' ') continue;
            u32 width=field_width(*p);
            if (!width || count>=sizeof layout->fields-1) return 0;
            layout->fields[count++]=*p;
            layout->stride+=width;
        }
        if (!count) return 0;
    } else if (kind==N64C_RES_RAW || kind==N64C_RES_SWAP16 || kind==N64C_RES_SWAP32) {
        if (fields) return 0;
        layout->stride=kind==N64C_RES_SWAP16 ? 2 : kind==N64C_RES_SWAP32 ? 4 : 1;
        layout->fields[0]=(char)('0'+layout->stride);
    } else {
        return 0;
    }
    return 1;
}
typedef struct { u32 at, unpacked, type; } Stream; /* a P node met by a walk: offset, unpacked bytes, type */
typedef u32 Planes[3]; /* an X node: records offset, count, F type */
static u8 *graph_widths(const Graph *graph,u32 root,const u8 *data,u32 bytes,Stream **streams,u32 *n_streams,
                       Planes **planes,u32 *n_planes);
static int disjoint(u32 lo,u32 hi)
{
    if (n_res>=MAX_RES || hi<=lo) return 0;
    for (int i=0;i<n_res;i++)
        if (lo<res[i].hi && hi>res[i].lo) return 0;
    return 1;
}
int n64c_pi_register(u32 lo,u32 hi,N64cResKind kind,const char *fields)
{
    if (!disjoint(lo,hi) || ((unsigned)kind>N64C_RES_ALCTL && kind!=N64C_RES_GRAPH && kind!=N64C_RES_UNTYPED))
        return -1; /* IMAGE, PACKED: own calls */
    Res entry={.lo=lo,.hi=hi,.kind=kind};
    if (kind==N64C_RES_UNTYPED) {
        if (!fields || !*fields) return -1;
        snprintf(entry.fields,sizeof entry.fields,"%s",fields);
        entry.stride=1;
    } else if (kind==N64C_RES_GRAPH) { /* the graph is walked over the ROM bytes once, into a per-byte width map */
        Graph *graph=parse_graph(fields);
        u8 *bytes=graph && rom && hi-lo<=0x01000000u ? malloc(hi-lo) : NULL;
        Stream *streams=NULL;
        u32 n_streams=0;
        int ok=bytes && fseek(rom,(long)lo,SEEK_SET)==0 && fread(bytes,1,hi-lo,rom)==hi-lo &&
            (entry.widths=graph_widths(graph,0,bytes,hi-lo,&streams,&n_streams,NULL,NULL))!=NULL;
        free(bytes);
        if (ok && n_nested+(int)n_streams>cap_nested) {
            int cap=cap_nested ? cap_nested : 64;
            while (cap<n_nested+(int)n_streams) cap*=2;
            Res *grown=realloc(nested,(size_t)cap*sizeof *grown);
            ok=grown!=NULL;
            if (ok) { nested=grown; cap_nested=cap; }
        }
        for (u32 i=0;ok && i<n_streams;i++) {
            Res stream={.lo=lo+streams[i].at,.hi=lo+streams[i].at+RNC_HEADER,.kind=N64C_RES_PACKED,.stride=1,
                        .unpacked_bytes=streams[i].unpacked};
            stream.unpacked.kind=streams[i].type==GRAPH_NONE ? N64C_RES_RAW : N64C_RES_GRAPH;
            stream.unpacked.graph=streams[i].type==GRAPH_NONE ? NULL : graph; /* owned by this range's entry */
            stream.unpacked.root=streams[i].type;
            nested[n_nested++]=stream;
        }
        free(streams);
        if (!ok) { free(entry.widths); free_graph(graph); return -1; }
        entry.unpacked.graph=graph; /* kept for the nested streams; freed with the entry */
        entry.stride=1;
    } else if (kind==N64C_RES_ALCTL) {
        if (fields || !rom || hi-lo>0x01000000u) return -1;
        u8 *bytes=malloc(hi-lo), *widths=malloc(hi-lo);
        int ok=bytes && widths && fseek(rom,(long)lo,SEEK_SET)==0 && fread(bytes,1,hi-lo,rom)==hi-lo &&
            n64c_al_ctl_widths(bytes,(s32)(hi-lo),widths);
        free(bytes);
        if (!ok) { free(widths); return -1; }
        entry.widths=widths;
        entry.stride=1;
    } else {
        Layout layout;
        if (!parse_layout(&layout,kind,fields)) return -1;
        memcpy(entry.fields,layout.fields,sizeof entry.fields);
        entry.stride=layout.stride;
        entry.tagged=layout.tagged;
    }
    if ((hi-lo)%entry.stride) return -1;
    res[n_res++]=entry;
    return 0;
}
int n64c_pi_register_image(u32 lo,u32 hi,const u8 *image)
{
    if (!disjoint(lo,hi) || !image) return -1;
    res[n_res++]=(Res){.lo=lo,.hi=hi,.kind=N64C_RES_IMAGE,.stride=1,.image=image};
    return 0;
}
static u32 be_value(const u8 *bytes,u32 width)
{
    u32 value=0;
    for (u32 i=0;i<width;i++) value=value<<8|bytes[i];
    return value;
}
int n64c_pi_register_packed(u32 lo,u32 hi,N64cResKind kind,const char *fields)
{
    static const u8 header_widths[]={4,4,4,2,2,1,1};
    u8 raw[RNC_HEADER];
    if (!disjoint(lo,hi) || !rom || hi-lo<RNC_HEADER || kind==N64C_RES_ALCTL || kind==N64C_RES_IMAGE ||
        kind==N64C_RES_PACKED) return -1;
    if (fseek(rom,(long)lo,SEEK_SET)!=0 || fread(raw,1,RNC_HEADER,rom)!=RNC_HEADER) return -1;
    if (raw[0]!='R' || raw[1]!='N' || raw[2]!='C' || (raw[3]!=1 && raw[3]!=2) ||
        be_value(raw+8,4)>hi-lo-RNC_HEADER) return -1;
    Res entry={.lo=lo,.hi=hi,.kind=N64C_RES_PACKED,.stride=1,.unpacked_bytes=be_value(raw+4,4)};
    for (u32 at=0,i=0;i<sizeof header_widths;at+=header_widths[i++])
        for (u32 b=0;b<header_widths[i];b++) entry.header[at+b]=raw[at+header_widths[i]-1-b];
    if (kind==N64C_RES_UNTYPED) {
        if (!fields || !*fields) return -1;
        snprintf(entry.fields,sizeof entry.fields,"%s",fields);
        entry.unpacked.kind=N64C_RES_UNTYPED;
    } else if (!parse_layout(&entry.unpacked,kind,fields)) return -1;
    res[n_res++]=entry;
    return 0;
}
static int convert_field(u8 *bytes,char field)
{
    u32 width=field_width(field);
    if (field=='p') {
        u32 original=(u32)bytes[0]<<24 | (u32)bytes[1]<<16 |
            (u32)bytes[2]<<8 | bytes[3];
        u32 native=0;
        if (original && (!relocate || !relocate(original,&native,relocate_user) || !native)) return 0;
        memcpy(bytes,&native,4);
    } else {
        for (u32 a=0,b=width-1;a<b;a++,b--) {
            u8 value=bytes[a];bytes[a]=bytes[b];bytes[b]=value;
        }
    }
    return 1;
}
/* Convert the fields of the record at ROM address record that lie inside [start, finish) of the buffer. */
static int fix_record(const char *fields,uint64_t record,uint64_t start,uint64_t finish,u32 dev,u8 *destination)
{
    uint64_t position=record;
    for (const char *field=fields;*field;field++) {
        u32 width=field_width(*field);
        uint64_t next=position+width;
        if (position<finish && next>start) {
            if (position<start || next>finish ||
                !convert_field(destination+(size_t)(position-dev),*field)) return 0;
        }
        position=next;
    }
    return 1;
}
/* The tag of the record at ROM address at, read from the file (big-endian); -1 when it is not readable. */
static int64_t record_tag(const Tagged *tagged,uint64_t at)
{
    u8 raw[4];
    if (fseek(rom,(long)(at+tagged->offset),SEEK_SET)!=0 || fread(raw,1,tagged->width,rom)!=tagged->width) return -1;
    int64_t value=0;
    for (u32 i=0;i<tagged->width;i++) value=value<<8|raw[i];
    return value;
}
static int fix_tagged(const Res *entry,uint64_t start,uint64_t finish,u32 dev,u8 *destination)
{
    const Tagged *tagged=entry->tagged;
    for (uint64_t record=entry->lo;record<finish;) {
        int64_t tag=record_tag(tagged,record);
        const TagLayout *layout=NULL;
        for (u32 i=0;tag>=0 && i<tagged->count;i++)
            if (tagged->layout[i].tag==(u32)tag) layout=&tagged->layout[i];
        if (!layout || record+layout->stride>entry->hi) return 0;
        uint64_t next=record+layout->stride;
        if (next>start && !fix_record(layout->fields,record,start,finish,dev,destination)) return 0;
        record=next;
    }
    return 1;
}
/* Swap each mapped field inside [start, finish); a field cut by either end refuses the read. */
static int fix_widths(const Res *entry,uint64_t start,uint64_t finish,u32 dev,u8 *destination)
{
    for (uint64_t at=start>entry->lo+3 ? start-3 : entry->lo;at<start;at++)
        if (at+entry->widths[at-entry->lo]>start) return 0;
    for (uint64_t at=start;at<finish;at++) {
        u32 width=entry->widths[at-entry->lo];
        if (!width) continue;
        if (at+width>finish) return 0;
        convert_field(destination+(size_t)(at-dev),(char)('0'+width));
    }
    return 1;
}
/* The byte-order gate: a DMA reached a blob nothing types. Stop naming it (exit 78, like a misfit layout), or with
 * N64C_LAYOUT_GATE=log name it once and serve its bytes raw (a diagnostic run that lists every untyped blob). */
static void untyped(Res *entry,u32 dev)
{
    const char *gate=getenv("N64C_LAYOUT_GATE");
    if (gate && strcmp(gate,"log")==0) {
        if (!entry->logged) fprintf(stderr,"boot-diag: untyped blob %s ROM 0x%08x\n",entry->fields,entry->lo);
        entry->logged=1;
        return;
    }
    fprintf(stderr,"boot-input: DMA of ROM 0x%08x reads blob %s (ROM 0x%08x-0x%08x), which has no byte-order layout: "
            "add a blob_layouts row or a byte_streams declaration to the manifest\n",dev,entry->fields,entry->lo,
            entry->hi);
    fflush(stderr);
    _Exit(78);
}
static int fixup(u32 dev,u8 *destination,u32 bytes)
{
    uint64_t end=(uint64_t)dev+bytes;
    for (int i=0;i<n_res;i++) {
        Res *entry=&res[i];
        uint64_t start=dev>entry->lo ? dev : entry->lo;
        uint64_t finish=end<entry->hi ? end : entry->hi;
        if (start>=finish || entry->kind==N64C_RES_RAW) continue;
        /* a read of the blob starts inside it; a bulk copy that only passes over it (a whole-cartridge DMA) is not */
        if ((entry->kind==N64C_RES_UNTYPED || (entry->kind==N64C_RES_PACKED && entry->unpacked.kind==N64C_RES_UNTYPED))
            && dev>=entry->lo)
            untyped(entry,dev);
        if (entry->kind==N64C_RES_UNTYPED) continue;
        if (entry->kind==N64C_RES_TAGGED) {
            if (!fix_tagged(entry,start,finish,dev,destination)) return 0;
            continue;
        }
        if (entry->kind==N64C_RES_ALCTL || entry->kind==N64C_RES_GRAPH) {
            if (!fix_widths(entry,start,finish,dev,destination)) return 0;
            continue;
        }
        if (entry->kind==N64C_RES_PACKED) {
            uint64_t header_end=entry->lo+RNC_HEADER;
            if (start<header_end)
                memcpy(destination+(size_t)(start-dev),entry->header+(size_t)(start-entry->lo),
                       (size_t)((finish<header_end ? finish : header_end)-start));
            continue;
        }
        if (entry->kind==N64C_RES_IMAGE) {
            memcpy(destination+(size_t)(start-dev),entry->image+(size_t)(start-entry->lo),(size_t)(finish-start));
            continue;
        }
        uint64_t record=entry->lo+(start-entry->lo)/entry->stride*entry->stride;
        for (;record<finish;record+=entry->stride) {
            if (!fix_record(entry->fields,record,start,finish,dev,destination)) return 0;
        }
    }
    return 1;
}
/* Drop the origins of RAM [lo, hi): entries it covers go, entries it cuts keep their outside parts. */
static void drop_origins(uintptr_t lo,uintptr_t hi)
{
    static Origin kept[MAX_ORIGIN];
    int n=0;
    for (int i=0;i<n_origin;i++) {
        Origin o=origin[i];
        if (o.hi<=lo || o.lo>=hi) { kept[n++]=o; continue; }
        if (o.lo<lo) kept[n++]=(Origin){o.lo,lo,o.dev};
        if (o.hi>hi && n<MAX_ORIGIN) kept[n++]=(Origin){hi,o.hi,o.dev+(u32)(hi-o.lo)};
    }
    memcpy(origin,kept,(size_t)n*sizeof origin[0]);
    n_origin=n;
}
static void remember(const void *ram,u32 bytes,u32 dev)
{
    uintptr_t lo=(uintptr_t)ram;
    if (!bytes) return;
    pthread_mutex_lock(&origin_lock);
    drop_origins(lo,lo+bytes);
    if (n_origin==MAX_ORIGIN) { /* the oldest goes */
        memmove(origin,origin+1,(MAX_ORIGIN-1)*sizeof origin[0]);
        n_origin--;
    }
    origin[n_origin++]=(Origin){lo,lo+bytes,dev};
    pthread_mutex_unlock(&origin_lock);
}
int n64c_pi_origin(const void *ram,u32 *dev)
{
    uintptr_t at=(uintptr_t)ram;
    int found=0;
    pthread_mutex_lock(&origin_lock);
    for (int i=n_origin-1;i>=0 && !found;i--)
        if (origin[i].lo<=at && at<origin[i].hi) {
            if (dev) *dev=origin[i].dev+(u32)(at-origin[i].lo);
            found=1;
        }
    pthread_mutex_unlock(&origin_lock);
    return found;
}
void n64c_pi_forget(const void *ram,u32 bytes)
{
    if (!bytes) return;
    pthread_mutex_lock(&origin_lock);
    drop_origins((uintptr_t)ram,(uintptr_t)ram+bytes);
    pthread_mutex_unlock(&origin_lock);
}
int n64c_pi_read(u32 address,void *destination,u32 bytes)
{
    if (!rom || !destination) return -1;
    if (fseek(rom, (long)(address & 0x0FFFFFFF), SEEK_SET) != 0)
        return -1;
    size_t got = fread(destination, 1, bytes, rom);
    if (got < bytes)
        memset((u8 *)destination + got, 0, bytes - got); /* past the end of the cartridge: open bus */
    if (!fixup(address & 0x0FFFFFFF,destination,bytes)) return -1;
    remember(destination,bytes,address & 0x0FFFFFFF);
    return 0;
}
/* Run-on diagnostics: the parts of a guest DMA no byte-order row (resources, loads, packed streams, overlay image)
 * covers. Those bytes reach the game big-endian; the first reader decides whether that is right (texels, ADPCM). */
static void layout_misses(u32 dev,const void *ram,u32 bytes)
{
    if (!n64c_diag_layout || !bytes) return;
    const uint64_t end=(uint64_t)dev+bytes;
    for (uint64_t cursor=dev;cursor<end;) {
        const Res *next=NULL;
        for (int i=0;i<n_res;i++)
            if (res[i].hi>cursor && res[i].lo<end && (!next || res[i].lo<next->lo)) next=&res[i];
        if (!next) {
            n64c_diag_layout("dma",(u32)cursor,(u32)(end-cursor),(const u8 *)ram+(cursor-dev));
            return;
        }
        if (next->lo>cursor)
            n64c_diag_layout("dma",(u32)cursor,(u32)(next->lo-cursor),(const u8 *)ram+(cursor-dev));
        if (next->hi>cursor) cursor=next->hi;
    }
}
/* A DMA the guest starts (osPiStartDma, osPiRawStartDma, the PI registers): the read, then the run-on check. */
int n64c_pi_dma(u32 address,void *destination,u32 bytes)
{
    if (n64c_pi_read(address,destination,bytes)!=0) return -1;
    layout_misses(address & 0x0FFFFFFF,destination,bytes);
    return 0;
}
extern void n64c_trace_event(const char *kind) __attribute__((weak)); /* boot census, when linked */
s32 osPiStartDma(OSIoMesg *io, s32 pri, s32 dir, u32 devAddr, void *dram, size_t n, OSMesgQueue *mq)
{
    (void)pri;
    if (dir != OS_READ || !rom || !dram)
        return -1;
    if (n>UINT32_MAX || n64c_pi_dma(devAddr,dram,(u32)n)!=0) return -1;
    if (n64c_trace_event) n64c_trace_event("first PI DMA");
    if (io)
        io->status = 0;
    OSMesg m;
    m.ptr = io;
    osSendMesg(mq ? mq : (io ? io->retQueue : NULL), m, OS_MESG_NOBLOCK);
    return 0;
}
/* The original programs the PI registers itself (dram, cart address, length) and learns completion
 * from the PI interrupt it registered with osSetEventMesg; the native read completes in the call. */
s32 osPiRawStartDma(s32 dir, u32 devAddr, void *dram, u32 n)
{
    if (dir != OS_READ || !rom || !dram || n64c_pi_dma(devAddr,dram,n)!=0)
        return -1;
    n64c_event_post(OS_EVENT_PI);
    return 0;
}
s32 osPiReadIo(u32 devAddr, u32 *data)
{
    u8 b[4];
    if (!rom || fseek(rom, (long)(devAddr & 0x0FFFFFFF), SEEK_SET) != 0 || fread(b, 1, 4, rom) != 4)
        return -1;
    *data = (u32)b[0] << 24 | (u32)b[1] << 16 | (u32)b[2] << 8 | b[3];
    return 0;
}
/* Unpacked bytes (big-endian, in a buffer) to host order by layout. 0 when the layout does not fit them. */
static int convert_run(const char *fields,u8 *data,u32 at,u32 end)
{
    for (const char *field=fields;*field;field++) {
        u32 width=field_width(*field);
        if (at+width>end || !convert_field(data+at,*field)) return 0;
        at+=width;
    }
    return 1;
}
static const TagLayout *tag_layout(const Tagged *tagged,const u8 *record,u32 room)
{
    if (tagged->offset+tagged->width>room) return NULL;
    u32 tag=be_value(record+tagged->offset,tagged->width);
    for (u32 i=0;i<tagged->count;i++)
        if (tagged->layout[i].tag==tag) return &tagged->layout[i];
    return NULL;
}
/* GRAPH walk: mark each typed byte's width in map (first byte w, the others 9), then convert by the map. */
typedef struct {
    const Graph *graph; const u8 *data; u8 *map; u32 bytes; u32 (*seen)[2]; u32 n_seen, cap_seen;
    Stream *streams; u32 n_streams, cap_streams; /* the P nodes met */
    Planes *planes; u32 n_planes, cap_planes; int planes_ok; /* the X nodes met; 0: a ROM range, X does not fit */
} Walk;
static int same_swapped(const u8 *bytes,u32 width)
{
    for (u32 a=0,b=width-1;a<b;a++,b--)
        if (bytes[a]!=bytes[b]) return 0;
    return 1;
}
/* Records may share bytes (the build tools reuse equal byte runs); two widths for one byte are only a conflict when
 * the order matters, i.e. unless both fields read the same swapped or not. */
static int mark(Walk *walk,u32 at,u32 width)
{
    if (at+width>walk->bytes) return 0;
    for (u32 i=0;i<width;i++) {
        u8 value=i ? 9 : (u8)width;
        if (walk->map[at+i] && walk->map[at+i]!=value) {
            u32 other=at+i;
            while (other>0 && walk->map[other]==9) other--;
            return same_swapped(walk->data+at,width) && walk->map[other]<=8 &&
                same_swapped(walk->data+other,walk->map[other]);
        }
    }
    memset(walk->map+at,9,width);
    walk->map[at]=(u8)width;
    return 1;
}
/* The section starts of the directory at [at, limit): count+1 offsets (the last its end) from at; 0 if malformed. */
static u32 *sections(const Walk *walk,u32 at,u32 limit,u32 *count,u32 *table)
{
    if (at+4>limit) return NULL;
    u32 n=be_value(walk->data+at,4);
    if (n>(limit-at-4)/4) return NULL;
    u32 first=n ? be_value(walk->data+at+4,4) : 4u+4u*n; /* the table runs to the first section */
    if (first<4u+4u*n || first>limit-at || first%4) return NULL;
    u32 *starts=malloc(((size_t)n+1)*sizeof *starts);
    if (!starts) return NULL;
    for (u32 i=0;i<n;i++) starts[i]=at+be_value(walk->data+at+4+4*i,4);
    starts[n]=limit;
    if (n && first>=4u+4u*(n+1)) {
        u32 end=be_value(walk->data+at+4+4*n,4);
        if (end<=limit-at && at+end>=starts[n-1]) starts[n]=at+end;
    }
    for (u32 i=0;i<n;i++)
        if (starts[i]<at+first || starts[i]>starts[i+1]) { free(starts); return NULL; }
    *count=n;
    *table=first;
    return starts;
}
static int walk_type(Walk *walk,u32 type,u32 at,u32 limit,const u32 *bases,u32 n_bases,int depth)
{
    const GType *t=&walk->graph->types[type];
    if (depth>64 || at>limit || limit>walk->bytes) return 0;
    for (u32 i=0;i<walk->n_seen;i++)
        if (walk->seen[i][0]==type && walk->seen[i][1]==at) return 1; /* shared record, typed once */
    if (walk->n_seen==walk->cap_seen) {
        u32 (*grown)[2]=realloc(walk->seen,(walk->cap_seen=walk->cap_seen ? walk->cap_seen*2 : 64)*sizeof *grown);
        if (!grown) return 0;
        walk->seen=grown;
    }
    walk->seen[walk->n_seen][0]=type;
    walk->seen[walk->n_seen++][1]=at;
    if (t->kind=='F') {
        if (at+t->size>limit) return 0;
        for (u32 i=0;i<t->n;i++) {
            u32 item=t->items[i];
            if (!(item&GRAPH_REF)) {
                if (!mark(walk,at,item)) return 0;
                at+=item;
                continue;
            }
            if (!mark(walk,at,4)) return 0;
            u32 offset=be_value(walk->data+at,4), section=(item>>16)&0x7FFF;
            at+=4;
            if (!offset || offset==0xFFFFFFFFu) continue;
            if (section>=n_bases) return 0;
            u32 target=bases[section]+offset, end=bases[section+1];
            if (offset>=end-bases[section] || !walk_type(walk,item&0xFFFF,target,end,bases,n_bases,depth+1))
                return 0;
        }
        return 1;
    }
    if (t->kind=='P') { /* a nested RNC stream: its header fields, the packed bytes raw */
        static const u8 header_widths[]={4,4,4,2,2,1,1};
        const u8 *h=walk->data+at;
        if (at+RNC_HEADER>limit || h[0]!='R' || h[1]!='N' || h[2]!='C' || (h[3]!=1 && h[3]!=2) ||
            be_value(h+8,4)>limit-at-RNC_HEADER) return 0;
        for (u32 i=0,p=at;i<sizeof header_widths;p+=header_widths[i++])
            if (!mark(walk,p,header_widths[i])) return 0;
        if (walk->n_streams==walk->cap_streams) {
            Stream *grown=realloc(walk->streams,(walk->cap_streams=walk->cap_streams ? walk->cap_streams*2 : 16)*
                                  sizeof *grown);
            if (!grown) return 0;
            walk->streams=grown;
        }
        walk->streams[walk->n_streams++]=(Stream){at,be_value(h+4,4),t->items[0]};
        return 1;
    }
    if (t->kind=='T') { /* the tag picks the record's variant */
        if (at+t->offset+t->width>limit) return 0;
        u32 tag=be_value(walk->data+at+t->offset,t->width);
        for (u32 k=0;k+1<t->n;k+=2)
            if (t->items[k]==tag) return walk_type(walk,t->items[k+1],at,limit,bases,n_bases,depth+1);
        return 0;
    }
    if (t->kind=='R') { /* a head record, then its element records to the end of the extent */
        const GType *head=&walk->graph->types[t->items[0]], *element=&walk->graph->types[t->items[1]];
        if (!walk_type(walk,t->items[0],at,limit,bases,n_bases,depth+1)) return 0;
        for (u32 p=at+head->size;p+element->size<=limit;p+=element->size)
            if (!walk_type(walk,t->items[1],p,limit,bases,n_bases,depth+1)) return 0;
        return 1;
    }
    if (t->kind=='X') { /* a leaf stored as byte planes: plane k holds byte k of every record */
        const GType *record=&walk->graph->types[t->items[0]];
        if (!walk->planes_ok || at+8>limit) return 0;
        u32 stride=be_value(walk->data+at,4), count=be_value(walk->data+at+4,4);
        if ((count && (stride!=record->size || count>(limit-at-8)/stride)) || !mark(walk,at,4) || !mark(walk,at+4,4))
            return 0;
        for (u32 p=at+8;p<at+8+stride*count;p++)
            if (!mark(walk,p,1)) return 0; /* claimed: the planes are reordered, never swapped in place */
        if (!count) return 1;
        if (walk->n_planes==walk->cap_planes) {
            Planes *grown=realloc(walk->planes,(walk->cap_planes=walk->cap_planes ? walk->cap_planes*2 : 8)*
                                    sizeof *grown);
            if (!grown) return 0;
            walk->planes=grown;
        }
        walk->planes[walk->n_planes][0]=at+8;
        walk->planes[walk->n_planes][1]=count;
        walk->planes[walk->n_planes++][2]=t->items[0];
        return 1;
    }
    if (t->kind=='L') {
        const GType *record=&walk->graph->types[t->items[0]];
        if (at+8>limit) return 0;
        u32 stride=be_value(walk->data+at,4), count=be_value(walk->data+at+4,4);
        if ((count && (stride!=record->size || count>(limit-at-8)/stride)) || !mark(walk,at,4) || !mark(walk,at+4,4))
            return 0;
        for (u32 i=0;i<count;i++)
            if (!walk_type(walk,t->items[0],at+8+i*stride,at+8+(i+1)*stride,bases,n_bases,depth+1)) return 0;
        return 1;
    }
    u32 count,table;
    u32 *starts=sections(walk,at,limit,&count,&table);
    if (!starts) return 0;
    int ok=1;
    for (u32 i=0;ok && i<table;i+=4) ok=mark(walk,at+i,4);
    for (u32 i=0;ok && i<count;i++) {
        u32 sub=GRAPH_NONE;
        if (t->kind=='D') {
            if (i<t->n) sub=t->items[i];
        } else if (t->kind=='E') {
            sub=t->items[0];
        } else if (starts[i]+t->offset+t->width<=starts[i+1]) {
            u32 tag=be_value(walk->data+starts[i]+t->offset,t->width);
            for (u32 k=0;k+1<t->n;k+=2)
                if (t->items[k]==tag) sub=t->items[k+1];
            ok=sub!=GRAPH_NONE; /* every tagged record has a type */
        } else {
            ok=0;
        }
        if (ok && sub!=GRAPH_NONE) ok=walk_type(walk,sub,starts[i],starts[i+1],starts,count,depth+1);
    }
    free(starts);
    return ok;
}
/* The width map of bytes typed by graph (each multi-byte field's width at its first byte, 0 elsewhere); NULL when
 * the graph does not fit the bytes. The plane leaves met go to planes; without planes (a ROM range, converted per DMA
 * piece) a plane leaf does not fit. */
static u8 *graph_widths(const Graph *graph,u32 root,const u8 *data,u32 bytes,Stream **streams,u32 *n_streams,
                       Planes **planes,u32 *n_planes)
{
    Walk walk={.graph=graph,.data=data,.bytes=bytes,.map=calloc(bytes ? bytes : 1,1),.planes_ok=planes!=NULL};
    if (!walk.map) return NULL;
    int ok=root<graph->count && walk_type(&walk,root,0,bytes,NULL,0,0);
    free(walk.seen);
    if (planes && ok) { *planes=walk.planes; *n_planes=walk.n_planes; }
    else free(walk.planes);
    if (streams) { *streams=walk.streams; *n_streams=walk.n_streams; }
    else free(walk.streams); /* streams nested in unpacked bytes are not DMA'd: only their headers are typed */
    if (!ok) { free(walk.map); if (streams) { free(*streams); *streams=NULL; *n_streams=0; } return NULL; }
    for (u32 at=0;at<bytes;at++)
        if (walk.map[at]==1 || walk.map[at]==9) walk.map[at]=0;
    return walk.map;
}
/* A plane leaf's records come back from the game's transpose (record byte k = plane k): a field of width w at offset
 * o is in host order when its planes o .. o+w-1 are reversed, so the transpose builds host-order records. */
static void reverse_planes(const GType *record,u8 *planes,u32 count)
{
    u32 offset=0;
    for (u32 i=0;i<record->n;offset+=record->items[i++])
        for (u32 a=offset,b=offset+record->items[i]-1;a<b;a++,b--)
            for (u32 k=0;k<count;k++) {
                u8 value=planes[a*count+k];planes[a*count+k]=planes[b*count+k];planes[b*count+k]=value;
            }
}
static int apply_graph(const Graph *graph,u32 root,u8 *data,u32 bytes)
{
    Planes *planes=NULL;
    u32 n_planes=0;
    u8 *widths=graph_widths(graph,root,data,bytes,NULL,NULL,&planes,&n_planes);
    if (!widths) return 0;
    for (u32 at=0;at<bytes;at++)
        if (widths[at]) convert_field(data+at,(char)('0'+widths[at]));
    for (u32 i=0;i<n_planes;i++) reverse_planes(&graph->types[planes[i][2]],data+planes[i][0],planes[i][1]);
    free(widths);
    free(planes);
    return 1;
}
static int apply_layout(const Layout *layout,u8 *data,u32 bytes)
{
    switch (layout->kind) {
    case N64C_RES_RAW: case N64C_RES_UNTYPED: /* UNTYPED: only after N64C_LAYOUT_GATE=log served it raw */
        return 1;
    case N64C_RES_SWAP16: case N64C_RES_SWAP32: case N64C_RES_STRUCT:
        if (bytes%layout->stride) return 0;
        for (u32 at=0;at<bytes;at+=layout->stride)
            if (!convert_run(layout->fields,data,at,at+layout->stride)) return 0;
        return 1;
    case N64C_RES_TAGGED:
        for (u32 at=0;at<bytes;) {
            const TagLayout *record=tag_layout(layout->tagged,data+at,bytes-at);
            if (!record || !convert_run(record->fields,data,at,bytes)) return 0;
            at+=record->stride;
        }
        return 1;
    case N64C_RES_GRAPH:
        return apply_graph(layout->graph,layout->root,data,bytes);
    default:
        return 0;
    }
}
static const Res *packed_stream(const void *ram,u32 skip)
{
    u32 dev;
    if (!n64c_pi_origin(ram,&dev) || dev<skip) return NULL;
    for (int i=0;i<n_res;i++)
        if (res[i].kind==N64C_RES_PACKED && res[i].lo==dev-skip) return &res[i];
    for (int i=0;i<n_nested;i++)
        if (nested[i].lo==dev-skip) return &nested[i];
    return NULL;
}
static void unpacked_layout(const Res *stream,const void *src,u32 skip,void *dst,s32 unpacked_size)
{
    n64c_pi_forget(dst,(u32)unpacked_size);
    if (!stream) {
        /* Run-on: an RNC stream no blob fact names leaves its unpacked bytes big-endian. */
        u32 dev;
        if (n64c_diag_layout)
            n64c_diag_layout("rnc",n64c_pi_origin(src,&dev) && dev>=skip ? dev-skip : 0xFFFFFFFFu,
                             (u32)unpacked_size,dst);
        return;
    }
    if ((u32)unpacked_size!=stream->unpacked_bytes || !apply_layout(&stream->unpacked,dst,(u32)unpacked_size)) {
        char subject[48];
        snprintf(subject,sizeof subject,"rnc-layout-0x%08x",stream->lo);
        if (n64c_diag_stop && n64c_diag_stop(subject,1)) return; /* run-on: the bytes stay as unpacked */
        fprintf(stderr,"boot-input: unpacked layout of the RNC stream at ROM 0x%08x does not fit its %d bytes\n",
                stream->lo,(int)unpacked_size);
        fflush(stderr);
        _Exit(78);
    }
}
void n64c_pi_rnc1_unpack(const void *src,void *dst,s32 packed_size,s32 unpacked_size)
{
    const Res *stream=packed_stream(src,RNC_HEADER);
    n64c_rnc1_unpack(src,dst,packed_size,unpacked_size);
    unpacked_layout(stream,src,RNC_HEADER,dst,unpacked_size);
}
void n64c_pi_rnc2_unpack(const void *header,void *dst,s32 packed_size,s32 unpacked_size)
{
    const Res *stream=packed_stream(header,0);
    u8 raw[RNC_HEADER];
    if (!stream) {
        n64c_rnc2_unpack(header,dst,packed_size,unpacked_size);
    } else {
        /* the header in RAM is in host order: unpack with the ROM's big-endian one */
        if (!rom || fseek(rom,(long)stream->lo,SEEK_SET)!=0 || fread(raw,1,RNC_HEADER,rom)!=RNC_HEADER)
            memset(raw,0,sizeof raw);
        n64c_rnc2_unpack_parts(raw,(const u8 *)header+RNC_HEADER,dst,packed_size,unpacked_size);
    }
    unpacked_layout(stream,header,0,dst,unpacked_size);
}
