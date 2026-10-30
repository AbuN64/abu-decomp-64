/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Call the real native implementations through the original C import ABI.
 * This TU, like a game unit, never includes the native shim type definitions. */
#include "../boot/n64c_guest_os.h"
#include "../boot/n64c_guest_al.h"
#include "../boot/n64c_guest_gu.h"
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>

struct OSMesgQueue {
    void **mtqueue, **fullqueue;
    int validCount, first, msgCount;
    void **msg;
};
struct ListLink {
    struct ListLink *next, *prev;
};

#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "guest imports:%d: %s\n", __LINE__, #condition); \
    exit(1); \
} } while (0)

int main(void)
{
    struct OSMesgQueue queue;
    OSMesg storage[2], received;
    int first = 17, second = 29;
    float matrix[4][4];
    struct ListLink head = {0, 0}, node = {0, 0};
    int row, column;
    _Static_assert(sizeof(void *) == 4, "guest imports require the 32-bit ABI");
    _Static_assert(sizeof(queue) == 24, "guest message queue size");
    _Static_assert(offsetof(struct OSMesgQueue, msg) == 20, "guest queue storage");
    osCreateMesgQueue(&queue, storage, 2);
    CHECK(osRecvMesg(&queue, &received, 0) == -1);
    CHECK(osSendMesg(&queue, &first, 0) == 0);
    CHECK(osSendMesg(&queue, &second, 0) == 0);
    CHECK(osSendMesg(&queue, &first, 0) == -1);
    CHECK(osRecvMesg(&queue, &received, 0) == 0 && received == &first);
    CHECK(osRecvMesg(&queue, &received, 0) == 0 && received == &second);
    CHECK(queue.validCount == 0);
    guMtxIdentF((unsigned char *)matrix);
    for (row = 0; row < 4; ++row)
        for (column = 0; column < 4; ++column)
            CHECK(matrix[row][column] == (row == column ? 1.0f : 0.0f));
    alLink(&node, (void **)&head);
    CHECK(head.next == &node && node.prev == &head);
    alUnlink(&node);
    CHECK(head.next == NULL);
    return 0;
}
