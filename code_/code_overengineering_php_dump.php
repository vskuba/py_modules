<?php
/**
 * Разбор PHP для code_overengineering: классы, методы, параметры, тела-обёртки и
 * вызовы своих методов — JSON в stdout. Правила судит Python, здесь только факты.
 *
 *     php code_overengineering_php_dump.php <vendor-dir> < список-файлов
 *
 * ⚠ Грузится только nikic/php-parser из vendor проекта, а не composer-автозагрузчик:
 * проектный autoload проверяет платформу (PHP 8.4) и падает на хостовом 7.4, а парсеру
 * версия рантайма не важна — синтаксис 8.x он разбирает и под 7.4.
 */

declare(strict_types=1);

$vendor = rtrim($argv[1] ?? '', '/');

spl_autoload_register(static function (string $class) use ($vendor): void {
    if (strncmp($class, 'PhpParser\\', 10) === 0) {
        $file = $vendor . '/nikic/php-parser/lib/' . str_replace('\\', '/', $class) . '.php';
        if (is_file($file)) {
            require $file;
        }
    }
});

use PhpParser\Node;
use PhpParser\NodeFinder;
use PhpParser\ParserFactory;
use PhpParser\PrettyPrinter\Standard;

$parser = (new ParserFactory())->createForNewestSupportedVersion();
$printer = new Standard();
$finder = new NodeFinder();
$out = [];

foreach (array_filter(array_map('trim', explode("\n", (string) stream_get_contents(STDIN)))) as $path) {
    try {
        $ast = $parser->parse((string) file_get_contents($path));
    } catch (Throwable $e) {
        continue;
    }

    $classes = [];

    foreach ($finder->findInstanceOf($ast ?? [], Node\Stmt\ClassLike::class) as $cls) {
        if ($cls->name === null) {
            continue;
        }

        $kind = $cls instanceof Node\Stmt\Interface_ ? 'interface'
            : ($cls instanceof Node\Stmt\Trait_ ? 'trait'
            : ($cls instanceof Node\Stmt\Enum_ ? 'enum'
            : ($cls instanceof Node\Stmt\Class_ && $cls->isAbstract() ? 'abstract' : 'class')));
        $methods = [];

        foreach ($cls->getMethods() as $m) {
            $params = [];
            foreach ($m->params as $p) {
                $params[] = [
                    'name' => $p->var instanceof Node\Expr\Variable ? (string) $p->var->name : '',
                    'default' => $p->default ? $printer->prettyPrintExpr($p->default) : null,
                    'variadic' => $p->variadic,
                    'promoted' => $p->flags !== 0,
                ];
            }

            $methods[] = [
                'name' => $m->name->toString(),
                'line' => $m->getStartLine(),
                'private' => $m->isPrivate(),
                'abstract' => $m->isAbstract() || $kind === 'interface',
                // Вход каркаса (маршрут, слушатель): зовётся не по имени — как декоратор в питоне.
                'routed' => $m->attrGroups !== []
                    || (bool) preg_match('/@(Rest\\\\|Route\\b|Method\\b|ParamConverter\\b)/', (string) $m->getDocComment()),
                'params' => $params,
                'forward' => _forward($m, $printer),
                'calls' => _own_calls($m, $finder, $printer),
            ];
        }

        $classes[] = [
            'name' => $cls->name->toString(),
            'kind' => $kind,
            'line' => $cls->getStartLine(),
            'methods' => $methods,
        ];
    }

    $out[$path] = $classes;
}

echo json_encode($out, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

/**
 * Тело — один `return вызов(...)` (или вызов без return): кому отдано и какие аргументы.
 * null — тело делает что-то своё.
 */
function _forward(Node\Stmt\ClassMethod $m, Standard $printer): ?array
{
    $stmts = $m->stmts ?? [];
    if (count($stmts) !== 1) {
        return null;
    }

    $one = $stmts[0];
    $expr = $one instanceof Node\Stmt\Return_ ? $one->expr
        : ($one instanceof Node\Stmt\Expression ? $one->expr : null);

    if (!$expr instanceof Node\Expr\CallLike || $expr->isFirstClassCallable()) {
        return null;
    }

    // ⚠ без `match`: скрипт гоняется хостовым PHP 7.4, а не рантаймом проекта.
    $target = '?';
    if ($expr instanceof Node\Expr\MethodCall || $expr instanceof Node\Expr\NullsafeMethodCall) {
        $target = $printer->prettyPrintExpr($expr->var) . '->'
            . ($expr->name instanceof Node\Identifier ? $expr->name->toString() : '?');
    } elseif ($expr instanceof Node\Expr\StaticCall) {
        $target = ($expr->class instanceof Node\Name ? $expr->class->toString() : '?') . '::'
            . ($expr->name instanceof Node\Identifier ? $expr->name->toString() : '?');
    } elseif ($expr instanceof Node\Expr\FuncCall && $expr->name instanceof Node\Name) {
        $target = $expr->name->toString();
    } elseif ($expr instanceof Node\Expr\New_ && $expr->class instanceof Node\Name) {
        $target = 'new ' . $expr->class->toString();
    }

    $args = [];
    foreach ($expr->getArgs() as $a) {
        $args[] = $a->value instanceof Node\Expr\Variable && !$a->unpack && $a->name === null
            ? (string) $a->value->name
            : null;
    }

    return ['target' => $target, 'args' => $args];
}

/**
 * Вызовы методов своего класса (`$this->m()`, `self::m()`, `static::m()`): имя и аргументы
 * — позиционные по порядку, именованные по имени; значение — литерал или null.
 */
function _own_calls(Node\Stmt\ClassMethod $m, NodeFinder $finder, Standard $printer): array
{
    $out = [];

    foreach ($finder->find($m->stmts ?? [], static fn (Node $n): bool => $n instanceof Node\Expr\MethodCall
        || $n instanceof Node\Expr\StaticCall) as $call) {
        $own = $call instanceof Node\Expr\MethodCall
            ? ($call->var instanceof Node\Expr\Variable && $call->var->name === 'this')
            : ($call->class instanceof Node\Name && in_array(strtolower($call->class->toString()), ['self', 'static'], true));

        if (!$own || !$call->name instanceof Node\Identifier || $call->isFirstClassCallable()) {
            continue;
        }

        $pos = [];
        $named = [];
        $opaque = false;

        foreach ($call->getArgs() as $a) {
            if ($a->unpack) {
                $opaque = true;
                break;
            }
            $value = _literal($a->value) ? $printer->prettyPrintExpr($a->value) : null;
            if ($a->name !== null) {
                $named[$a->name->toString()] = ['given' => true, 'value' => $value];
            } else {
                $pos[] = ['given' => true, 'value' => $value];
            }
        }

        $out[] = ['name' => $call->name->toString(), 'pos' => $pos, 'named' => (object) $named, 'opaque' => $opaque];
    }

    return $out;
}

/** Литерал ли выражение: скаляр, константа true/false/null, массив литералов. */
function _literal(Node\Expr $e): bool
{
    if ($e instanceof Node\Scalar && !$e instanceof Node\Scalar\InterpolatedString) {
        return true;
    }
    if ($e instanceof Node\Expr\ConstFetch) {
        return in_array(strtolower($e->name->toString()), ['true', 'false', 'null'], true);
    }
    if ($e instanceof Node\Expr\ClassConstFetch) {
        return true;
    }
    if ($e instanceof Node\Expr\UnaryMinus) {
        return _literal($e->expr);
    }
    if ($e instanceof Node\Expr\Array_) {
        foreach ($e->items as $item) {
            if ($item === null || !_literal($item->value) || ($item->key && !_literal($item->key))) {
                return false;
            }
        }
        return true;
    }
    return false;
}
